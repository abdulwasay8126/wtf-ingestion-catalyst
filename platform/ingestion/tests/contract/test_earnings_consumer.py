from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings
from wtf_ingestion.earnings import EarningsUnavailable, earnings_data, fiscal_structure
from wtf_ingestion.operations.web import create_app
from wtf_ingestion.persistence.database import Base
from wtf_ingestion.persistence.models import CollectionRun, SourceAssertion
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher
from wtf_ingestion.registry import seed_registry


def accepted_earnings(session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(output_dir=tmp_path / "out", raw_dir=tmp_path / "raw")
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    candidate = SecEarningsAdapter(cfg).parse_fixture(raw, datetime.now(UTC))[0]
    service = IngestionService(session, cfg.raw_dir)
    service.ingest(
        source_key="sec_edgar",
        candidates=[candidate],
        raw=raw,
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    result = Publisher(session, cfg.output_dir).publish()
    return cfg, result, candidate, service


def test_exact_published_fields_evidence_and_visible_missing_issuers(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    cfg, publication, _, _ = accepted_earnings(db_session, fixture_root, tmp_path)
    research = earnings_data(db_session, cfg)
    row = research["data"][0]
    assert research["publication_id"] == str(publication.publication_id)
    assert row["company_id"] and row["event_revision"] == row["event"]["revision_number"]
    assert row["fields"]["release_time"]["value"]
    assert row["fiscal"]["quarter"] == 4 and row["fiscal"]["year"] == 2026
    assert row["evidence"][0]["evidence_id"] and row["evidence"][0]["raw_sha256"]
    assert row["brief"]["state"] == "not_generated"
    assert row["reaction"]["state"] == "unavailable"
    assert research["counts"]["missing_issuers"] == 4


@pytest.mark.parametrize(
    "filename", ["events.jsonl", "events.csv", "manifest.json", "current.json"]
)
def test_consumer_fails_closed_for_tampered_publication(
    db_session, fixture_root, tmp_path, filename
):  # type: ignore[no-untyped-def]
    cfg, publication, _, _ = accepted_earnings(db_session, fixture_root, tmp_path)
    path = (
        cfg.output_dir / filename
        if filename == "current.json"
        else publication.directory / filename
    )
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(EarningsUnavailable):
        earnings_data(db_session, cfg)


def test_consumer_retains_exact_old_revision_after_unpublished_change(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    cfg, _, candidate, service = accepted_earnings(db_session, fixture_root, tmp_path)
    original = candidate.event_date
    revised = candidate.model_copy(
        update={
            "event_date": original + timedelta(days=1),
            "scheduled_start": candidate.scheduled_start + timedelta(days=1),
            "collected_at": datetime.now(UTC) + timedelta(seconds=1),
        }
    )
    service.ingest(
        source_key="sec_edgar",
        candidates=[revised],
        raw=b"revised fixture",
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    with pytest.raises(EarningsUnavailable, match="DEGRADED"):
        earnings_data(db_session, cfg)
    research = earnings_data(db_session, cfg, retained=True)
    assert not research["is_current"]
    row = research["data"][0]
    assert row["event"]["event_date"] == original.isoformat()
    assert row["event_revision"] == 1


def test_stale_default_refuses_current_but_retained_has_explicit_trust(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    cfg, _, _, _ = accepted_earnings(db_session, fixture_root, tmp_path)
    run = db_session.scalar(select(CollectionRun))
    run.freshness_deadline = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    with pytest.raises(EarningsUnavailable, match="trust"):
        earnings_data(db_session, cfg)
    research = earnings_data(db_session, cfg, retained=True)
    assert not research["is_current"] and research["calendar_state"] == "STALE"


def test_latest_alternate_filing_does_not_replace_published_exhibit_evidence(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    cfg, _, candidate, _ = accepted_earnings(db_session, fixture_root, tmp_path)
    original = db_session.scalar(select(SourceAssertion))
    alternate = candidate.model_copy(
        update={"source_event_id": candidate.source_event_id + ":primary", "title": "Alternate"}
    )
    db_session.add(
        SourceAssertion(
            run_id=original.run_id,
            source_id=original.source_id,
            event_id=original.event_id,
            source_event_id=alternate.source_event_id,
            facts=alternate.model_dump(mode="json"),
            observed_at=candidate.collected_at + timedelta(microseconds=1),
            evidence_strength=original.evidence_strength,
            raw_payload_hash=original.raw_payload_hash,
        )
    )
    db_session.commit()
    row = earnings_data(db_session, cfg)["data"][0]
    assert row["evidence"][0]["evidence_id"] == str(original.id)


def test_reparse_with_original_observation_binds_to_exact_parser_revision(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    cfg, _, candidate, service = accepted_earnings(db_session, fixture_root, tmp_path)
    revised = candidate.model_copy(update={"parser_version": "new-parser"})
    service.ingest(
        source_key="sec_edgar",
        candidates=[revised],
        raw=b"same facts reparsed",
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    retained = earnings_data(db_session, cfg, retained=True)["data"][0]
    assert retained["event"]["parser_version"] == candidate.parser_version
    Publisher(db_session, cfg.output_dir).publish()
    current = earnings_data(db_session, cfg)["data"][0]
    assert current["event"]["parser_version"] == "new-parser"
    assertion = db_session.get(SourceAssertion, UUID(current["evidence"][0]["evidence_id"]))
    assert assertion.facts["parser_version"] == "new-parser"
    assert assertion.facts["collected_at"] == candidate.collected_at.isoformat().replace(
        "+00:00", "Z"
    )


@pytest.mark.parametrize(
    "period,quarter,year",
    [
        ("Q3 2026", 3, 2026),
        ("Q1 FY2027", 1, 2027),
        ("Q4 and FY2026", 4, 2026),
        (None, None, None),
        ("unknown", None, None),
    ],
)
def test_fiscal_structure_only_accepts_source_backed_period(period, quarter, year):
    actual = fiscal_structure(period)
    assert (actual["quarter"], actual["year"]) == (quarter, year)


def test_api_uses_accepted_snapshot_without_dashboard(fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    # The HTTP app uses another connection. Use a committed isolated database,
    # rather than the PostgreSQL fixture's deliberately uncommitted savepoint.
    database_url = f"sqlite+pysqlite:///{tmp_path / 'consumer-web.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_registry(session)
        cfg, publication, _, _ = accepted_earnings(session, fixture_root, tmp_path)
    cfg.database_url = database_url
    with TestClient(create_app(cfg)) as client:
        response = client.get("/v1/earnings?ticker=AAPL&limit=1")
        assert response.status_code == 200
        data = response.json()
        assert data["pagination"]["total"] == 1 and len(data["data"]) == 1
        assert data["publication_id"] == str(publication.publication_id)
        assert "why_it_matters" in data["data"][0]["brief"]["sections"]
        page = client.get("/earnings?ticker=AAPL")
        assert page.status_code == 503
        assert "dashboard assets are not installed" in page.json()["detail"]
        assert data["data"][0]["event"]["fiscal_period"] == "Q4 FY2026"
        assert data["data"][0]["brief"]["state"] == "not_generated"
        assert client.get("/v1/earnings?limit=0").status_code == 422
    assert (
        json.loads((cfg.output_dir / "current.json").read_text())["publication_id"]
        == data["publication_id"]
    )
    engine.dispose()
