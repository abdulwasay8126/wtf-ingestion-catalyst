from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from wtf_ingestion.adapters.base import PayloadError, TransportError
from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings
from wtf_ingestion.domain.models import EventCandidate
from wtf_ingestion.operations.queries import coverage, overview
from wtf_ingestion.persistence.models import (
    CollectionRun,
    EventCurrent,
    EventVersion,
    QuarantineRecord,
    RawObject,
    SourceAssertion,
)
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher


def candidate(fixture_root):  # type: ignore[no-untyped-def]
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    return raw, SecEarningsAdapter(Settings()).parse_fixture(raw, datetime.now(UTC))[0]


def ingest(service, raw, record):  # type: ignore[no-untyped-def]
    return service.ingest(
        source_key=record.source_id,
        candidates=[record],
        raw=raw,
        source_url=str(record.source_url),
        content_type="application/json",
    )


def test_issuer_wins_and_all_assertions_survive(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    raw, sec = candidate(fixture_root)
    service = IngestionService(db_session, tmp_path / "raw")
    ingest(service, raw, sec)
    issuer = EventCandidate.model_validate(
        {
            **sec.model_dump(),
            "source_id": "issuer_demo",
            "source_event_id": "issuer-explicit",
            "title": "Issuer confirmation",
        }
    )
    assert ingest(service, b"issuer", issuer).revised == 1
    assert ingest(service, raw, sec).revised == 0
    current = db_session.scalar(select(EventCurrent))
    assert current is not None and current.source_key == "issuer_demo"
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 1
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 2
    assert db_session.scalar(select(func.count()).select_from(SourceAssertion)) == 3


def test_conflicting_duplicates_quarantine_entire_batch(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    raw, record = candidate(fixture_root)
    changed = EventCandidate.model_validate({**record.model_dump(), "title": "Different facts"})
    service = IngestionService(db_session, tmp_path / "raw")
    result = service.ingest(
        source_key="sec_edgar",
        candidates=[record, changed],
        raw=raw,
        source_url=str(record.source_url),
        content_type="application/json",
    )
    assert result.state == "DEGRADED"
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 0
    assert db_session.scalar(select(func.count()).select_from(QuarantineRecord)) == 1


def test_exact_duplicates_are_counted(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    raw, record = candidate(fixture_root)
    result = IngestionService(db_session, tmp_path / "raw").ingest(
        source_key="sec_edgar",
        candidates=[record, record],
        raw=raw,
        source_url=str(record.source_url),
        content_type="application/json",
    )
    run = db_session.get(CollectionRun, result.run_id)
    assert run and run.duplicate_count == 1 and run.accepted_count == 1


@pytest.mark.parametrize("stage", ["fetch", "parse"])
def test_failures_get_a_run_ledger_and_raw_before_parse(db_session, tmp_path, stage):  # type: ignore[no-untyped-def]
    service = IngestionService(db_session, tmp_path / "raw")

    def fetch(run_id):  # type: ignore[no-untyped-def]
        if stage == "fetch":
            raise TransportError("not a public error")
        return b"malformed", "https://data.sec.gov/test", "application/json"

    def parse(raw, observed):  # type: ignore[no-untyped-def]
        raise PayloadError("not a public error")

    with pytest.raises((TransportError, PayloadError)):
        service.collect(source_key="sec_edgar", fetch=fetch, parse=parse, evidence_mode="fixture")
    run = db_session.scalar(select(CollectionRun))
    assert run and run.state == "FAILED" and run.stage == stage
    assert "not a public" not in (run.safe_error_summary or "")
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == (
        1 if stage == "parse" else 0
    )


@pytest.mark.parametrize(
    "bad_state", ["FAILED", "SUSPICIOUS_EMPTY", "DEGRADED", "STALE", "RUNNING"]
)
def test_publication_refuses_bad_refresh_and_preserves_pointer(
    db_session, fixture_root, tmp_path, bad_state
):  # type: ignore[no-untyped-def]
    raw, record = candidate(fixture_root)
    service = IngestionService(db_session, tmp_path / "raw", output_dir=tmp_path / "out")
    first = ingest(service, raw, record)
    publisher = Publisher(db_session, tmp_path / "out")
    publisher.publish()
    before = (tmp_path / "out/current.json").read_bytes()
    run = db_session.get(CollectionRun, first.run_id)
    assert run
    run.state = bad_state if bad_state != "STALE" else "HEALTHY"
    if bad_state == "STALE":
        run.freshness_deadline = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    with pytest.raises(RuntimeError, match="refused"):
        publisher.publish()
    assert (tmp_path / "out/current.json").read_bytes() == before
    assert overview(db_session)["feed_state"] != "HEALTHY"


def test_evidence_cannot_be_relabelled_live(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    raw, record = candidate(fixture_root)
    ingest(IngestionService(db_session, tmp_path / "raw"), raw, record)
    with pytest.raises(RuntimeError):
        Publisher(db_session, tmp_path / "out").publish("live")


def test_source_local_date_survives_utc_rollover(fixture_root):  # type: ignore[no-untyped-def]
    _, record = candidate(fixture_root)
    data = record.model_dump()
    data.update(
        scheduled_start="2026-10-30T02:00:00Z",
        event_date="2026-10-29",
        source_timezone="America/New_York",
    )
    result = EventCandidate.model_validate(data)
    assert result.event_date.isoformat() == "2026-10-29"


def test_stale_and_cancelled_do_not_count_as_confirmed_coverage(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    raw, record = candidate(fixture_root)
    service = IngestionService(db_session, tmp_path / "raw")
    result = ingest(service, raw, record)
    assert coverage(db_session)["counts"]["confirmed"] == 1
    run = db_session.get(CollectionRun, result.run_id)
    assert run
    run.freshness_deadline = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert coverage(db_session)["counts"]["source_unavailable"] == 1
    cancelled = EventCandidate.model_validate({**record.model_dump(), "status": "cancelled"})
    ingest(service, raw, cancelled)
    assert coverage(db_session)["counts"]["not_yet_announced"] == 5
