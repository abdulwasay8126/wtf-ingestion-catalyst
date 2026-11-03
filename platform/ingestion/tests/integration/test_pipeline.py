from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select

from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings
from wtf_ingestion.domain.models import EventCandidate
from wtf_ingestion.persistence.models import CollectionRun, EventCurrent, EventVersion
from wtf_ingestion.pipeline import IngestionService

OBSERVED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_idempotency_revision_and_failed_empty_preserve_current(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    candidate = SecEarningsAdapter(Settings()).parse_fixture(raw, OBSERVED)[0]
    service = IngestionService(db_session, tmp_path / "raw")
    first = service.ingest(
        source_key="sec_edgar",
        candidates=[candidate],
        raw=raw,
        source_url="https://www.sec.gov/Archives/edgar/data/fixture",
        content_type="application/json",
    )
    second = service.ingest(
        source_key="sec_edgar",
        candidates=[candidate],
        raw=raw,
        source_url="https://www.sec.gov/Archives/edgar/data/fixture",
        content_type="application/json",
    )
    assert first.inserted == 1
    assert second.unchanged == 1
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 1
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 1

    changed_data = candidate.model_dump(mode="python")
    changed_data["event_date"] = date(2026, 10, 30)
    changed_data["scheduled_start"] = None
    changed_data["time_precision"] = "date_only"
    changed_data["status"] = "moved"
    changed = EventCandidate.model_validate(changed_data)
    revised = service.ingest(
        source_key="sec_edgar",
        candidates=[changed],
        raw=b"revised sanitized fixture",
        source_url="https://www.sec.gov/Archives/edgar/data/fixture",
        content_type="application/json",
    )
    assert revised.revised == 1
    current = db_session.scalar(select(EventCurrent))
    assert current is not None and current.revision_number == 2
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 2

    empty = service.ingest(
        source_key="sec_edgar",
        candidates=[],
        raw=b'{"filings": []}',
        source_url="https://data.sec.gov/submissions/fixture.json",
        content_type="application/json",
    )
    assert empty.state == "SUSPICIOUS_EMPTY"
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 1
    failed_run = db_session.get(CollectionRun, empty.run_id)
    assert failed_run is not None
    assert failed_run.safe_error_code == "UNEXPECTED_ZERO_RECORDS"
