from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.adapters.sec_estimates import project_next_earnings
from wtf_ingestion.adapters.sec_native import Filing, parse_exhibit
from wtf_ingestion.config import Settings
from wtf_ingestion.earnings import EarningsUnavailable, earnings_data
from wtf_ingestion.earnings_evidence import (
    EvidenceUnavailable,
    encoded,
    pack_directory,
    publish_evidence_pack,
    read_evidence_pack,
    retained_body,
)
from wtf_ingestion.operations.web import create_app
from wtf_ingestion.persistence.database import Base
from wtf_ingestion.persistence.models import CollectionRun
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher
from wtf_ingestion.registry import seed_registry


def save_raw(cfg, raw):  # type: ignore[no-untyped-def]
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    (cfg.raw_dir / f"{digest}.bin").write_bytes(raw)
    return digest


def accepted(session, fixture_root, tmp_path, *, kind="fixture", change=None):  # type: ignore[no-untyped-def]
    cfg = Settings(output_dir=tmp_path / "out", raw_dir=tmp_path / "raw")
    observed = datetime.now(UTC)
    if kind == "fixture":
        raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
        candidate = SecEarningsAdapter(cfg).parse_fixture(raw, observed)[0]
    elif kind == "estimate":
        raw = encoded(
            {
                "cik": "320193",
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-25-000001"],
                        "filingDate": ["2025-10-30"],
                        "form": ["8-K"],
                        "items": ["2.02"],
                        "primaryDocument": ["release.htm"],
                    }
                },
            }
        )
        save_raw(cfg, raw)
        candidate = project_next_earnings(
            raw,
            cik="0000320193",
            ticker="AAPL",
            exchange="NASDAQ",
            company_name="Apple Inc.",
            observed=observed,
        ).candidates[0]
    else:
        body = (
            b"<html><body>Apple Inc. will release its fourth quarter fiscal 2026 results "
            b"on October 29, 2026 after the market close and will host a conference call "
            b"on October 29, 2026 at 5:00 p.m. ET. This is invented test evidence.</body></html>"
        )
        filing = Filing("0000320193", "0000320193-26-000099", date(2026, 9, 30), "8-K", "ex.htm")
        url = filing.root_url + "ex.htm"
        document = {
            "url": url,
            "body_sha256": save_raw(cfg, body),
            "filing": {**asdict(filing), "filed_on": filing.filed_on.isoformat()},
            "ticker": "AAPL",
            "exchange": "NASDAQ",
            "company_name": "Apple Inc.",
            "retrieved_at": observed.isoformat(),
        }
        bundle = {"complete": True, "documents": [document]}
        candidate = parse_exhibit(
            body,
            filing=filing,
            url=url,
            ticker="AAPL",
            exchange="NASDAQ",
            company_name="Apple Inc.",
            observed=observed,
        )[0]
        if change:
            change(bundle)
        raw = encoded(bundle)
    service = IngestionService(session, cfg.raw_dir, allow_estimates=kind == "estimate")
    service.ingest(
        source_key="sec_edgar",
        candidates=[candidate],
        raw=raw,
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    publication = Publisher(session, cfg.output_dir).publish()
    return cfg, publication, candidate, service


@pytest.mark.parametrize("kind", ["fixture", "native", "estimate"])
def test_source_verified_pack_is_immutable_and_idempotent(db_session, fixture_root, tmp_path, kind):  # type: ignore[no-untyped-def]
    cfg, publication, _, _ = accepted(db_session, fixture_root, tmp_path, kind=kind)
    with patch("wtf_ingestion.adapters.base.SafeHttpClient.get", side_effect=AssertionError):
        first = publish_evidence_pack(db_session, cfg)
        second = publish_evidence_pack(db_session, cfg)
        pack = read_evidence_pack(db_session, cfg)
    assert not first["reused"] and second["reused"]
    assert first["sha256"] == second["sha256"]
    assert pack["is_current"] and pack["evidence_mode"] == "fixture"
    assert pack["publication_id"] == str(publication.publication_id)
    assert len(pack["records"]) == 1 and len(pack["missing_issuers"]) == 4
    assert not pack["briefs_generated"] and not pack["prices_included"]
    row = pack["records"][0]
    assert all(
        claim["evidence_ids"] == [row["evidence"][0]["evidence_id"]] for claim in row["claims"]
    )
    proof = row["evidence"][0]
    if kind == "native":
        assert proof["document_sha256"] != proof["source_bundle_sha256"]
        assert "October 29, 2026" in proof["excerpt"]
        assert proof["locator"]["end"] - proof["locator"]["start"] == len(proof["excerpt"])
        call = next(claim for claim in row["claims"] if claim["field"] == "conference_call")
        assert call["value"]["state"] == "available"
        assert "release_time" in row["unavailable_fields"]
    elif kind == "estimate":
        assert proof["excerpt"] is None and proof["document_sha256"] is None
        assert proof["calculation"]["output_estimated_date"] == "2026-10-29"
        assert not proof["calculation"]["actual_release_date"]
        assert row["claims"][0]["kind"] == "calculated_estimate"


@pytest.mark.parametrize("filename", ["pack.json", "manifest.json"])
def test_reader_and_rebuild_refuse_tampered_pack(db_session, fixture_root, tmp_path, filename):  # type: ignore[no-untyped-def]
    cfg, publication, _, _ = accepted(db_session, fixture_root, tmp_path)
    publish_evidence_pack(db_session, cfg)
    path = pack_directory(cfg, str(publication.publication_id)) / filename
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceUnavailable):
        read_evidence_pack(db_session, cfg)
    with pytest.raises(EvidenceUnavailable):
        publish_evidence_pack(db_session, cfg)
    assert path.read_text() == "{}"  # Failed rebuild never overwrites evidence.


@pytest.mark.parametrize(
    "damage", ["duplicate", "wrong_date", "wrong_identity", "incomplete", "ocr"]
)
def test_native_pack_refuses_unbound_or_unapproved_evidence(
    db_session, fixture_root, tmp_path, damage
):  # type: ignore[no-untyped-def]
    def change(bundle):  # type: ignore[no-untyped-def]
        document = bundle["documents"][0]
        if damage == "duplicate":
            bundle["documents"].append(document.copy())
        elif damage == "wrong_identity":
            document["filing"]["cik"] = "0000789019"
        elif damage == "incomplete":
            bundle["complete"] = False
        elif damage == "ocr":
            document["ocr_sha256"] = "a" * 64

    cfg, publication, candidate, service = accepted(
        db_session, fixture_root, tmp_path, kind="native", change=change
    )
    if damage == "wrong_date":
        original = earnings_data(db_session, cfg)["data"][0]["evidence"][0]["raw_sha256"]
        revised = candidate.model_copy(
            update={"event_date": candidate.event_date + timedelta(days=1)}
        )
        service.ingest(
            source_key="sec_edgar",
            candidates=[revised],
            raw=retained_body(cfg, original),
            source_url="https://data.sec.gov/test",
            content_type="application/json",
        )
        publication = Publisher(db_session, cfg.output_dir).publish()
    with pytest.raises(EvidenceUnavailable):
        publish_evidence_pack(db_session, cfg)
    assert not pack_directory(cfg, str(publication.publication_id)).exists()


def test_estimate_must_reproduce_the_whole_accepted_projection(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg, _, candidate, service = accepted(db_session, fixture_root, tmp_path, kind="estimate")
    altered = json.loads(retained_body(cfg, candidate.metadata["estimate_basis_raw_sha256"]))
    altered["filings"]["recent"]["form"] = ["8-K/A"]
    raw = encoded(altered)
    revised = candidate.model_copy(
        update={"metadata": {**candidate.metadata, "estimate_basis_raw_sha256": save_raw(cfg, raw)}}
    )
    service.ingest(
        source_key="sec_edgar",
        candidates=[revised],
        raw=raw,
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    Publisher(db_session, cfg.output_dir).publish()
    with pytest.raises(EvidenceUnavailable, match="filing basis"):
        publish_evidence_pack(db_session, cfg)


@pytest.mark.parametrize("damage", ["tamper", "missing", "oversize"])
def test_retained_input_failures_never_publish(db_session, fixture_root, tmp_path, damage):  # type: ignore[no-untyped-def]
    cfg, publication, _, _ = accepted(db_session, fixture_root, tmp_path)
    digest = earnings_data(db_session, cfg)["data"][0]["evidence"][0]["raw_sha256"]
    path = cfg.raw_dir / f"{digest}.bin"
    if damage == "tamper":
        path.write_bytes(b"incorrect")
    elif damage == "missing":
        path.rename(path.with_suffix(".retained"))
    else:
        cfg.max_response_bytes = 1000
    with pytest.raises(EvidenceUnavailable):
        publish_evidence_pack(db_session, cfg)
    assert not pack_directory(cfg, str(publication.publication_id)).exists()


def test_stale_pack_needs_explicit_retained_request_and_cannot_be_rebuilt(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    cfg, _, _, _ = accepted(db_session, fixture_root, tmp_path)
    publish_evidence_pack(db_session, cfg)
    run = db_session.scalar(select(CollectionRun))
    run.freshness_deadline = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    with pytest.raises(EarningsUnavailable, match="STALE"):
        read_evidence_pack(db_session, cfg)
    with pytest.raises(EarningsUnavailable):
        publish_evidence_pack(db_session, cfg)
    assert not read_evidence_pack(db_session, cfg, retained=True)["is_current"]


def test_pack_does_not_follow_an_older_publication_implicitly(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg, _, _, _ = accepted(db_session, fixture_root, tmp_path)
    publish_evidence_pack(db_session, cfg)
    Publisher(db_session, cfg.output_dir).publish()
    with pytest.raises(EvidenceUnavailable):
        read_evidence_pack(db_session, cfg)


def test_atomic_failure_preserves_calendar_and_withholds_pack(
    db_session, fixture_root, tmp_path, monkeypatch
):  # type: ignore[no-untyped-def]
    cfg, publication, _, _ = accepted(db_session, fixture_root, tmp_path)
    pointer = (cfg.output_dir / "current.json").read_bytes()
    monkeypatch.setattr(
        Path, "rename", lambda *args: (_ for _ in ()).throw(OSError("disk failure"))
    )
    with pytest.raises(EvidenceUnavailable):
        publish_evidence_pack(db_session, cfg)
    assert (cfg.output_dir / "current.json").read_bytes() == pointer
    assert not pack_directory(cfg, str(publication.publication_id)).exists()
    assert earnings_data(db_session, cfg)["is_current"]


def test_structural_source_errors_are_actionable_pack_failures(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg, _, candidate, service = accepted(db_session, fixture_root, tmp_path)
    malformed = json.loads((fixture_root / "sec/explicit_announcement.json").read_bytes())
    malformed["filings"] = {}
    service.ingest(
        source_key="sec_edgar",
        candidates=[candidate],
        raw=encoded(malformed),
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    Publisher(db_session, cfg.output_dir).publish()
    with pytest.raises(EvidenceUnavailable, match="validation/publication"):
        publish_evidence_pack(db_session, cfg)


def test_api_reads_only_generated_pack_and_keeps_fixture_out_of_live(fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    url = f"sqlite+pysqlite:///{tmp_path / 'pack-api.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_registry(session)
        cfg, _, _, _ = accepted(session, fixture_root, tmp_path)
        cfg.database_url = url
        with TestClient(create_app(cfg)) as client:
            assert client.get("/v1/earnings/evidence").status_code == 503
        publish_evidence_pack(session, cfg)
    with TestClient(create_app(cfg)) as client:
        result = client.get("/v1/earnings/evidence?ticker=AAPL&limit=1")
        assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        assert result.json()["pagination"] == {"offset": 0, "limit": 1, "total": 1}
        assert client.get("/v1/earnings/evidence?ticker=MSFT").json()["records"] == []
        assert client.get("/v1/earnings/evidence?limit=0").status_code == 422
    cfg.dashboard_mode = "live"
    with TestClient(create_app(cfg)) as client:
        assert client.get("/v1/earnings/evidence?retained=true").status_code == 503
    engine.dispose()


@pytest.mark.parametrize("damage", ["excerpt", "claim", "identity", "mode", "duplicate"])
def test_valid_file_checksums_do_not_allow_unbound_facts_or_excerpts(
    db_session, fixture_root, tmp_path, damage
):  # type: ignore[no-untyped-def]
    cfg, publication, _, _ = accepted(db_session, fixture_root, tmp_path, kind="native")
    publish_evidence_pack(db_session, cfg)
    directory = pack_directory(cfg, str(publication.publication_id))
    payload = json.loads((directory / "pack.json").read_bytes())
    row = payload["records"][0]
    if damage == "excerpt":
        row["evidence"][0]["excerpt"] = "Invented unsupported earnings result."
    elif damage == "claim":
        row["claims"][0]["value"] = "2026-11-01"
    elif damage == "identity":
        payload["identity_basis"]["assumption"] = False
    elif damage == "mode":
        payload["evidence_mode"] = "live"
    else:
        payload["records"].append(row.copy())
    row["evidence_pack_id"] = hashlib.sha256(
        encoded({key: value for key, value in row.items() if key != "evidence_pack_id"})
    ).hexdigest()
    body = encoded(payload)
    (directory / "pack.json").write_bytes(body)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["sha256"] = hashlib.sha256(body).hexdigest()
    manifest["records"] = len(payload["records"])
    (directory / "manifest.json").write_bytes(encoded(manifest))
    with pytest.raises(EvidenceUnavailable):
        read_evidence_pack(db_session, cfg)
