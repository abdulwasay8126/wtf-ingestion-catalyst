from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import func, select, text

from wtf_ingestion.adapters.base import (
    FetchPolicy,
    IncompleteScanError,
    PayloadError,
    SafeHttpClient,
    ThrottledError,
)
from wtf_ingestion.collectors import SecCollector
from wtf_ingestion.config import Settings
from wtf_ingestion.operations.queries import scan_preview
from wtf_ingestion.operations.web import create_app
from wtf_ingestion.persistence.models import (
    CollectionRun,
    EventCurrent,
    EventVersion,
    SecCompanyCheckpoint,
    SecDocumentParse,
    Source,
)
from wtf_ingestion.registry import seed_registry


def client_for(
    fixture_root,
    visited,
    *,
    bad=True,
    throttle=False,
    new_filing=False,
    primary_body=None,
):  # type: ignore[no-untyped-def]
    def respond(request):  # type: ignore[no-untyped-def]
        visited.append(str(request.url))
        if "submissions/" in str(request.url):
            cik = request.url.path.rsplit("/", 1)[-1][3:-5]
            payload = json.loads((fixture_root / "sec/submissions_native.json").read_bytes())
            payload["cik"] = cik
            if cik == "0000320193" and new_filing:
                recent = payload["filings"]["recent"]
                for key, value in {
                    "accessionNumber": "0000320193-26-000100",
                    "filingDate": "2026-10-01",
                    "form": "8-K",
                    "items": "8.01,9.01",
                    "primaryDocument": "announcement.htm",
                }.items():
                    recent[key].insert(0, value)
            if cik != "0000320193":
                for key in payload["filings"]["recent"]:
                    payload["filings"]["recent"][key] = []
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("-index.html"):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=(fixture_root / "sec/document_index.html")
                .read_bytes()
                .replace(
                    b"000032019326000099",
                    b"000032019326000100"
                    if "000032019326000100" in request.url.path
                    else b"000032019326000099",
                ),
            )
        if request.url.path.endswith("announcement.htm"):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=primary_body or b"<p>Refer to the earnings release furnished herewith.</p>",
            )
        if throttle:
            return httpx.Response(429)
        if "000032019326000100" in request.url.path:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<p>Historical results were filed. This is a sanitized test report.</p>",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=(
                b"<DOCUMENT><TYPE>EX-99.1<TEXT><HTML><HEAD>"
                b"<TITLE>Supplemental report with metadata</TITLE></HEAD>"
                b"<BODY><IMG src='cover.jpg'></BODY></HTML></TEXT></DOCUMENT>"
                if bad
                else (fixture_root / "sec/exhibit_native.html").read_bytes()
            ),
        )

    return SafeHttpClient(
        FetchPolicy(
            frozenset({"data.sec.gov", "www.sec.gov"}),
            "Offline test",
            min_interval_seconds=0,
            stop_on_throttle=True,
        ),
        transport=httpx.MockTransport(respond),
    )


def test_company_failure_continues_and_retry_resumes(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    visited = []
    client = client_for(fixture_root, visited)
    try:
        with pytest.raises(IncompleteScanError):
            SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    first = db_session.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()))
    assert first.state == "FAILED" and first.safe_error_code == "SEC_SCAN_INCOMPLETE"
    checks = db_session.scalars(
        select(SecCompanyCheckpoint).where(SecCompanyCheckpoint.run_id == first.id)
    ).all()
    assert len(checks) == 5
    assert sum(row.state == "VALIDATED" for row in checks) == 4
    review = next(row for row in checks if row.state == "REVIEW_REQUIRED")
    assert review.error_code == "DOCUMENT_TEXT_UNREADABLE"
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 0
    assert scan_preview(db_session)["checked"] == 5
    before = len(visited)
    client = client_for(fixture_root, visited)
    try:
        with pytest.raises(IncompleteScanError):
            SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    # Fresh metadata is always checked; only unchanged good documents are reused.
    # Recheck the previously unreadable exhibit too so source corrections can recover.
    assert len(visited) - before == 6
    preview = scan_preview(db_session)
    assert preview["resumed"] == 0 and len(preview["reviews"]) == 1
    assert preview["reused_objects"] > 0 and preview["network_requests"] == 6
    assert preview["reused_parses"] == 1
    latest = db_session.scalars(
        select(SecCompanyCheckpoint).where(SecCompanyCheckpoint.run_id == preview["run"].id)
    ).all()
    for checkpoint in latest:
        if checkpoint.reused_from_id:
            original = db_session.get(SecCompanyCheckpoint, checkpoint.reused_from_id)
            assert checkpoint.checked_at == original.checked_at


def test_unchanged_endpoint_validation_avoids_repeated_commits(
    db_session, fixture_root, tmp_path, monkeypatch
):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    visited = []
    client = client_for(fixture_root, visited, bad=False)
    try:
        SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    client = client_for(fixture_root, visited, bad=False)
    collector = SecCollector(cfg, db_session, client)
    collector.run(evidence_mode="fixture")
    client.close()
    url = next(url for url in collector.captured)
    # The final company's metadata has already been validated and committed.
    commits = []
    original = db_session.commit

    def commit():  # type: ignore[no-untyped-def]
        commits.append(True)
        original()

    monkeypatch.setattr(db_session, "commit", commit)
    collector._remember_validated(url, collector.run_id)
    assert not commits
    assert collector._endpoint_cache(url) is collector._endpoint_cache(url)


def test_fixture_reseed_cannot_reset_real_sec_parser_or_rate(db_session):  # type: ignore[no-untyped-def]
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.fixture_only = False
    source.live_enabled = False  # Includes a blocked/disabled real source.
    source.parser_version = "real-parser-version"
    source.rate_policy = "operator-reviewed runtime rate"
    db_session.commit()
    seed_registry(db_session)
    db_session.commit()
    assert source.parser_version == "real-parser-version"
    assert source.rate_policy == "operator-reviewed runtime rate"
    assert not source.live_enabled


def test_resume_expiry_rechecks_metadata(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    visited = []
    for instant in ["2026-10-01T12:00:00Z", "2026-10-01T19:00:00Z"]:
        with freeze_time(instant, tick=True):
            client = client_for(fixture_root, visited)
            try:
                with pytest.raises(IncompleteScanError):
                    SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
            finally:
                client.close()
    assert sum("submissions/" in url for url in visited) == 10
    assert scan_preview(db_session)["resumed"] == 0


def test_source_throttle_still_stops_immediately(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    visited = []
    client = client_for(fixture_root, visited, throttle=True)
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    try:
        with pytest.raises(ThrottledError):
            SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    assert not db_session.scalars(
        select(SecCompanyCheckpoint).where(SecCompanyCheckpoint.state == "REVIEW_REQUIRED")
    ).all()
    assert len(db_session.scalars(select(SecCompanyCheckpoint)).all()) < 5


def test_checkpoint_history_is_immutable(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    visited = []
    client = client_for(fixture_root, visited, bad=False)
    try:
        SecCollector(Settings(raw_dir=tmp_path / "raw"), db_session, client).run(
            evidence_mode="fixture"
        )
    finally:
        client.close()
    with pytest.raises(Exception, match="append-only"):
        db_session.execute(text("DELETE FROM sec_company_checkpoints"))
    db_session.rollback()


def test_preview_expires_without_promoting_to_feed(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    visited = []
    client = client_for(fixture_root, visited, bad=False)
    try:
        SecCollector(Settings(raw_dir=tmp_path / "raw"), db_session, client).run(
            evidence_mode="fixture"
        )
    finally:
        client.close()
    assert len(scan_preview(db_session)["candidates"]) == 1
    with freeze_time("2026-10-01T19:00:00Z"):
        preview = scan_preview(db_session)
        assert preview["stale"] == 5 and preview["candidates"] == []
        assert len(preview["retained_candidates"]) == 1
        assert preview["retained_candidates"][0].collected_at < datetime.now(UTC)


def test_source_fields_and_expired_candidates_stay_audit_only(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    client = client_for(fixture_root, [], bad=False)
    try:
        SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    preview = scan_preview(db_session)
    candidate = preview["candidates"][0]
    assert candidate.ticker == "AAPL"
    assert candidate.exchange == "NASDAQ"
    assert candidate.cik == "0000320193"
    assert candidate.fiscal_period == "Q4 FY2026"
    assert candidate.event_type.value == "earnings_release"
    assert candidate.source_url and candidate.collected_at.tzinfo is not None
    with freeze_time("2026-10-01T19:00:00Z"):
        expired = scan_preview(db_session)
        assert expired["candidates"] == []
        assert len(expired["retained_candidates"]) == 1
        assert expired["retained_candidates"][0].material_dict() == candidate.material_dict()
        assert expired["stale"] == 5
    assert len(scan_preview(db_session)["candidates"]) == 1


def test_explicit_recheck_can_recover_changed_bad_document(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    visited = []
    client = client_for(fixture_root, visited)
    try:
        with pytest.raises(IncompleteScanError):
            SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    before = len(visited)
    cfg.sec_force_recheck = True
    client = client_for(fixture_root, visited, bad=False)
    try:
        result = SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    assert result.state == "HEALTHY" and result.inserted == 1
    assert len(visited) - before == 8
    assert scan_preview(db_session)["resumed"] == 0


def test_retired_preview_route_does_not_expose_staged_candidates(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    cfg = Settings(database_url=str(db_session.get_bind().engine.url), raw_dir=tmp_path / "raw")
    source_client = client_for(fixture_root, [], bad=False)
    try:
        SecCollector(cfg, db_session, source_client).run(evidence_mode="fixture")
    finally:
        source_client.close()
    assert len(scan_preview(db_session)["candidates"]) == 1
    with TestClient(create_app(cfg)) as browser:
        for suffix in ("?ticker=AAPL", "?ticker=MSFT", "?status=estimated"):
            assert browser.get("/scan/preview" + suffix).status_code == 404
    assert len(scan_preview(db_session)["candidates"]) == 1


def test_unchanged_document_comparison_and_new_filing_detection(
    db_session,
    fixture_root,
    tmp_path,
    monkeypatch,
):  # type: ignore[no-untyped-def]
    import wtf_ingestion.collectors as module

    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    visited = []
    parsed = []
    original = module.parse_exhibit

    def spy(body, **kwargs):  # type: ignore[no-untyped-def]
        parsed.append(kwargs["url"])
        return original(body, **kwargs)

    monkeypatch.setattr(module, "parse_exhibit", spy)
    for new_filing, expected_requests, expected_parses in [
        (False, 8, 2),
        (False, 5, 0),
        (True, 8, 2),
    ]:
        before_requests, before_parses = len(visited), len(parsed)
        client = client_for(fixture_root, visited, bad=False, new_filing=new_filing)
        try:
            result = SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
        finally:
            client.close()
        assert result.state == "HEALTHY"
        assert len(visited) - before_requests == expected_requests
        assert len(parsed) - before_parses == expected_parses
        if before_requests:
            assert all(
                "submissions/" in url or "000032019326000100" in url
                for url in visited[before_requests:]
            )
            assert scan_preview(db_session)["reused_parses"] == 2
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 1
    assert db_session.scalar(select(func.count()).select_from(SecDocumentParse)) == 4


def test_comparison_checksum_context_parser_expiry_and_history(
    db_session,
    fixture_root,
    tmp_path,
    monkeypatch,
):  # type: ignore[no-untyped-def]
    import wtf_ingestion.collectors as module

    cfg = Settings(raw_dir=tmp_path / "raw")
    client = client_for(fixture_root, [], bad=False)
    try:
        SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    checkpoint = next(
        row
        for row in db_session.scalars(select(SecCompanyCheckpoint)).all()
        if row.payload["documents"]
    )
    document = checkpoint.payload["documents"][1]
    body = (fixture_root / "sec/exhibit_native.html").read_bytes()
    collector = SecCollector(cfg, db_session)
    collector.run_id = checkpoint.run_id
    collector.evidence_mode = "fixture"
    observed = datetime(2026, 10, 1, 13, tzinfo=UTC)
    events = collector._parse_document(body, document, observed)
    assert collector.reused_parses == 1
    assert events[0].collected_at.isoformat() == document["retrieved_at"]
    with pytest.raises(PayloadError, match="checksum"):
        collector._parse_document(body + b"changed", document, observed)
    changed = body.replace(b"October 29", b"October 30")
    changed_doc = {**document, "body_sha256": hashlib.sha256(changed).hexdigest()}
    assert collector._parse_document(changed, changed_doc, observed)[0].event_date.day == 30
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(SecDocumentParse)) == 3
    assert collector._parse_document(body, document, observed)[0].event_date.day == 29
    assert collector._parse_document(body, document, datetime(2026, 10, 30, tzinfo=UTC)) == []
    original = module.parse_exhibit
    parsed = []

    def spy(raw, **kwargs):  # type: ignore[no-untyped-def]
        parsed.append(kwargs["url"])
        return original(raw, **kwargs)

    monkeypatch.setattr(module, "parse_exhibit", spy)
    collector._parse_document(body, document, datetime(2026, 9, 30, tzinfo=UTC))
    collector._parse_document(body, {**document, "company_name": "Changed name"}, observed)
    db_session.commit()
    monkeypatch.setattr(module, "NATIVE_PARSER_VERSION", "sec-native-next-test")
    collector._parse_document(body, document, observed)
    db_session.commit()
    assert len(parsed) == 3
    for statement in [
        "DELETE FROM sec_document_parses",
        "UPDATE sec_document_parses SET payload='[]'",
    ]:
        with pytest.raises(Exception, match="append-only"):
            db_session.execute(text(statement))
        db_session.rollback()


def test_fixed_source_review_is_not_refetched_forever(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw")
    visited = []
    for bad in [True, False, False]:
        client = client_for(fixture_root, visited, bad=bad)
        try:
            if bad:
                with pytest.raises(IncompleteScanError):
                    SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
            else:
                assert (
                    SecCollector(cfg, db_session, client).run(evidence_mode="fixture").state
                    == "HEALTHY"
                )
        finally:
            client.close()
    assert len(visited) == 8 + 6 + 5


def test_parser_upgrade_revalidates_retained_bytes_without_redownloading(
    db_session,
    fixture_root,
    tmp_path,
    monkeypatch,
):  # type: ignore[no-untyped-def]
    import wtf_ingestion.collectors as module

    cfg = Settings(raw_dir=tmp_path / "raw")
    visited, parsed = [], []
    original = module.parse_exhibit
    current_version = module.NATIVE_PARSER_VERSION

    def spy(body, **kwargs):  # type: ignore[no-untyped-def]
        parsed.append(kwargs["url"])
        return original(body, **kwargs)

    monkeypatch.setattr(module, "parse_exhibit", spy)
    for version in ["sec-native-older-test", current_version]:
        monkeypatch.setattr(module, "NATIVE_PARSER_VERSION", version)
        client = client_for(fixture_root, visited, bad=False)
        try:
            assert (
                SecCollector(cfg, db_session, client).run(evidence_mode="fixture").state
                == "HEALTHY"
            )
        finally:
            client.close()
    assert len(visited) == 8 + 5
    assert len(parsed) == 4  # New parser must not trust earlier normalized facts.
    assert db_session.scalar(select(func.count()).select_from(SecDocumentParse)) == 4
    assert scan_preview(db_session)["reused_objects"] == 3


def test_saved_body_obeys_current_size_bound(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw")
    visited = []
    large = b"<p>" + b"Historical report with explicit readable body. " * 100 + b"</p>"
    client = client_for(fixture_root, visited, bad=False, primary_body=large)
    try:
        assert SecCollector(cfg, db_session, client).run(evidence_mode="fixture").state == "HEALTHY"
    finally:
        client.close()
    before = len(visited)
    cfg.max_response_bytes = 1000
    client = client_for(fixture_root, visited, bad=False, primary_body=large)
    try:
        with pytest.raises(IncompleteScanError):
            SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    assert len(visited) - before == 5
    # Retained-reference validation already rejects oversized files before _get.
    assert scan_preview(db_session)["reviews"][0]["code"] == "PAYLOAD_ERROR"
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 1
