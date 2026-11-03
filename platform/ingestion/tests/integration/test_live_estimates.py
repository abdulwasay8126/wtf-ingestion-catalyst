from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select, text

from wtf_ingestion.adapters.sec_estimates import project_next_earnings
from wtf_ingestion.domain.models import EventCandidate
from wtf_ingestion.operations.queries import coverage
from wtf_ingestion.persistence.models import EventCurrent, EventVersion, Source, SourceAssertion
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher


def projection():  # type: ignore[no-untyped-def]
    raw = json.dumps(
        {
            "cik": "320193",
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000001"],
                    "filingDate": ["2025-10-30"],
                    "form": ["8-K"],
                    "items": ["2.02,9.01"],
                    "primaryDocument": ["release.htm"],
                }
            },
        }
    ).encode()
    event = project_next_earnings(
        raw,
        cik="0000320193",
        ticker="AAPL",
        exchange="NASDAQ",
        company_name="Sanitized issuer",
        observed=datetime.now(UTC),
    ).candidates[0]
    return raw, event


def ingest(db_session, tmp_path, event, raw, *, allow=True, mode="fixture"):  # type: ignore[no-untyped-def]
    from wtf_ingestion.persistence.models import CollectionRun
    from wtf_ingestion.universe import active_universe

    result = IngestionService(db_session, tmp_path / "raw", allow_estimates=allow).ingest(
        source_key="sec_edgar",
        candidates=[event],
        raw=raw,
        source_url="https://data.sec.gov/submissions/CIK0000320193.json",
        content_type="application/json",
        evidence_mode=mode,
    )
    # Model the complete fixed-scope collection boundary in this synthetic evidence test.
    db_session.get(CollectionRun, result.run_id).universe_version_id = active_universe(
        db_session
    ).id
    db_session.commit()
    return result


def test_disabled_estimates_rejected_without_current_mutation(db_session, tmp_path):  # type: ignore[no-untyped-def]
    raw, event = projection()
    with pytest.raises(ValueError, match="disabled"):
        ingest(db_session, tmp_path, event, raw, allow=False)
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"session": "AMC"},
        {"fiscal_period": "FY2026Q4"},
        {"event_date": date(2026, 10, 30)},
        {"source_url": "https://www.sec.gov/Archives/edgar/data/320193/wrong/release.htm"},
        {"metadata": {}},
        {"metadata_method": "made_up"},
    ],
)
def test_invalid_estimate_preserves_last_good(db_session, tmp_path, change):  # type: ignore[no-untyped-def]
    raw, event = projection()
    ingest(db_session, tmp_path, event, raw)
    payload = event.model_dump(mode="python")
    if "metadata_method" in change:
        payload["metadata"]["estimate_method"] = change["metadata_method"]
    else:
        payload.update(change)
    bad = EventCandidate.model_validate(payload)
    with pytest.raises(ValueError):
        ingest(db_session, tmp_path, bad, raw)
    assert db_session.scalar(select(EventCurrent)).event_date == date(2026, 10, 29)
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 1


def test_live_estimate_feed_provenance_retention_and_coverage(db_session, tmp_path):  # type: ignore[no-untyped-def]
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.live_enabled, source.fixture_only, source.permission_status = True, False, "approved"
    db_session.commit()
    raw, event = projection()
    first = ingest(db_session, tmp_path, event, raw, mode="live")
    second = ingest(db_session, tmp_path, event, raw, mode="live")
    assert first.inserted == 1 and second.unchanged == 1
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 1
    assert db_session.scalar(select(SourceAssertion)).evidence_strength == "derived_estimate"
    assert coverage(db_session)["counts"]["estimated"] == 1
    assert coverage(db_session)["counts"].get("confirmed", 0) == 0
    result = Publisher(db_session, tmp_path / "out").publish("live")
    record = json.loads((result.directory / "events.jsonl").read_text())
    manifest = json.loads((result.directory / "manifest.json").read_text())
    assert record["date_basis"] == "estimated" and record["estimate_confidence"] == "low"
    assert record["estimate_basis_date"] == "2025-10-30"
    assert manifest["schema_version"] == "0.2.0"
    assert manifest["date_basis_counts"] == {
        "estimated": 1,
        "announced": 0,
        "official_schedule": 0,
    }
    assert manifest["history_policy"] == {"retain_at_least_years": 2, "initial_backfill": False}
    if db_session.get_bind().dialect.name == "postgresql":
        row = db_session.execute(text("SELECT * FROM events_feed_current")).mappings().one()
        assert (
            row["date_basis"] == "estimated" and row["estimate_method"] == record["estimate_method"]
        )
        assert row["estimate_basis_date"].isoformat() == record["estimate_basis_date"]
    with freeze_time("2026-10-03T12:00:00Z"):
        assert coverage(db_session)["counts"]["source_unavailable"] == 1
        with pytest.raises(RuntimeError, match="STALE"):
            Publisher(db_session, tmp_path / "out").publish("live")
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 1


def test_announced_date_replaces_active_projection_without_guessing_identity(db_session, tmp_path):  # type: ignore[no-untyped-def]
    raw, event = projection()
    ingest(db_session, tmp_path, event, raw)
    payload = event.model_dump(mode="python")
    payload.update(
        status="confirmed",
        source_event_id="official-next-date",
        title="Sanitized explicit official announcement",
        event_date=date(2026, 10, 30),
        metadata={},
        parser_version="sec-native-v2",
    )
    official = EventCandidate.model_validate(payload)
    ingest(db_session, tmp_path, official, b"sanitized official date")
    result = Publisher(db_session, tmp_path / "out").publish()
    record = json.loads((result.directory / "events.jsonl").read_text())
    assert result.record_count == 1 and record["date_basis"] == "announced"
    assert record["event_date"] == "2026-10-30" and record["fiscal_period"] is None
    assert coverage(db_session)["counts"]["announced_date_only"] == 1
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 2
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 2


def test_fixture_estimate_cannot_be_relabelled_live(db_session, tmp_path):  # type: ignore[no-untyped-def]
    raw, event = projection()
    ingest(db_session, tmp_path, event, raw)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.live_enabled, source.fixture_only, source.permission_status = True, False, "approved"
    db_session.commit()
    IngestionService(db_session, tmp_path / "raw").ingest(
        source_key="sec_edgar",
        candidates=[],
        raw=b"sanitized live empty observation",
        source_url="https://data.sec.gov/submissions/CIK0000320193.json",
        content_type="application/json",
        evidence_mode="live",
        allow_empty=True,
    )
    with pytest.raises(RuntimeError, match="mixed or relabelled"):
        Publisher(db_session, tmp_path / "out").publish("live")
