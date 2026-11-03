from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select, text

from wtf_ingestion.adapters import prefetch
from wtf_ingestion.adapters.base import FetchPolicy, RequestGate, SafeHttpClient
from wtf_ingestion.collectors import SecCollector
from wtf_ingestion.config import Settings
from wtf_ingestion.persistence.models import CollectionRun, EventCurrent, HttpAttempt, RawObject


@pytest.mark.parametrize("parallel_archives", [False, True])
def test_native_collector_full_offline_run(
    db_session, fixture_root, tmp_path, monkeypatch, parallel_archives
):  # type: ignore[no-untyped-def]
    visited = []

    def respond(request):  # type: ignore[no-untyped-def]
        visited.append(str(request.url))
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        if "submissions/" in str(request.url):
            cik = request.url.path.rsplit("/", 1)[-1].removeprefix("CIK").removesuffix(".json")
            payload = json.loads((fixture_root / "sec/submissions_native.json").read_bytes())
            payload["cik"] = cik
            if cik != "0000320193":
                for key in payload["filings"]["recent"]:
                    payload["filings"]["recent"][key] = []
            return httpx.Response(200, headers={"etag": '"fixture-v1"'}, json=payload)
        if request.url.path.endswith("-index.html"):
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "etag": '"fixture-v1"'},
                content=(fixture_root / "sec/document_index.html").read_bytes(),
            )
        if request.url.path.endswith("announcement.htm"):
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "etag": '"fixture-v1"'},
                content=b"<p>See the furnished press release for further information.</p>",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "etag": '"fixture-v1"'},
            content=(fixture_root / "sec/exhibit_native.html").read_bytes(),
        )

    client = SafeHttpClient(
        FetchPolicy(
            frozenset({"data.sec.gov", "www.sec.gov"}), "Fixture Test", min_interval_seconds=0
        ),
        transport=httpx.MockTransport(respond),
    )
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    monkeypatch.setattr(
        prefetch,
        "SafeHttpClient",
        lambda policy, **kwargs: SafeHttpClient(
            policy, transport=httpx.MockTransport(respond), **kwargs
        ),
    )

    def collect():
        collector = SecCollector(cfg, db_session, client)
        if parallel_archives:
            gate = RequestGate(0)
            client.gate = gate
            collector.archive_prefetch = prefetch.MetadataPrefetch([], client.policy, gate, 2)
        return collector.run(evidence_mode="fixture")

    try:
        result = collect()
        second = collect()
    finally:
        client.close()
    assert result.state == "HEALTHY" and result.inserted == 1
    assert second.state == "HEALTHY" and second.unchanged == 1
    assert len(visited) == 13  # 8 first scan; 5 metadata checks, 3 saved archive bodies reused.
    assert db_session.scalar(select(func.count()).select_from(HttpAttempt)) == 13
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == 18
    assert db_session.scalar(select(EventCurrent)).parser_version == "sec-native-v15"


def test_collector_disabled_does_not_request(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    with pytest.raises(RuntimeError, match="disabled"):
        SecCollector(cfg, db_session).run()
    assert db_session.scalar(select(func.count()).select_from(CollectionRun)) == 0


def test_projection_collector_uses_real_shape_and_revalidated_raw(db_session, tmp_path):  # type: ignore[no-untyped-def]
    def respond(request):  # type: ignore[no-untyped-def]
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        cik = request.url.path.rsplit("/", 1)[-1].removeprefix("CIK").removesuffix(".json")
        return httpx.Response(
            200,
            headers={"etag": '"projection-v1"'},
            json={
                "cik": cik,
                "filings": {
                    "recent": {
                        "accessionNumber": [f"{cik}-25-000001"],
                        "filingDate": ["2025-10-30"],
                        "form": ["8-K"],
                        "items": ["2.02"],
                        "primaryDocument": ["release.htm"],
                    }
                },
            },
        )

    client = SafeHttpClient(
        FetchPolicy(
            frozenset({"data.sec.gov", "www.sec.gov"}),
            "Fixture Test",
            min_interval_seconds=0,
        ),
        transport=httpx.MockTransport(respond),
    )
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out", enable_sec_estimates=True)
    try:
        first = SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
        second = SecCollector(cfg, db_session, client).run(evidence_mode="fixture")
    finally:
        client.close()
    assert first.inserted == 5 and second.unchanged == 5
    events = db_session.scalars(select(EventCurrent)).all()
    assert all(event.status == "estimated" for event in events)
    assert db_session.scalar(select(func.count()).select_from(HttpAttempt)) == 10
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == 12


@pytest.mark.parametrize("table", ["event_versions", "source_assertions", "raw_objects"])
def test_immutable_history_blocks_mutation(db_session, fixture_root, tmp_path, table):  # type: ignore[no-untyped-def]
    from wtf_ingestion.adapters.sec import SecEarningsAdapter
    from wtf_ingestion.pipeline import IngestionService

    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    records = SecEarningsAdapter(Settings()).parse_fixture(raw, datetime.now(UTC))
    IngestionService(db_session, tmp_path / "raw").ingest(
        source_key="sec_edgar",
        candidates=records,
        raw=raw,
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    with pytest.raises(Exception, match="append-only"):
        statements = {
            "event_versions": "DELETE FROM event_versions",
            "source_assertions": "DELETE FROM source_assertions",
            "raw_objects": "DELETE FROM raw_objects",
        }
        db_session.execute(text(statements[table]))
    db_session.rollback()
