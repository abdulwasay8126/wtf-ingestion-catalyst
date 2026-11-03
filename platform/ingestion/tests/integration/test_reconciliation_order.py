from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings
from wtf_ingestion.domain.models import EventCandidate
from wtf_ingestion.persistence.models import (
    EventCurrent,
    EventVersion,
    QuarantineRecord,
    RawObject,
    SourceAssertion,
)
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher


def announcements(fixture_root):  # type: ignore[no-untyped-def]
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    base = SecEarningsAdapter(Settings()).parse_fixture(raw, datetime.now(UTC))[0]
    old = EventCandidate.model_validate(
        {
            **base.model_dump(),
            "source_event_id": "old-filing",
            "event_date": "2026-10-29",
            "scheduled_start": None,
            "time_precision": "date_only",
            "source_published_at": "2026-09-29T10:00:00Z",
            "metadata": {"filed_on": "2026-09-29"},
        }
    )
    new = EventCandidate.model_validate(
        {
            **old.model_dump(),
            "source_event_id": "new-filing",
            "event_date": "2026-10-30",
            "source_published_at": "2026-09-30T10:00:00Z",
            "metadata": {"filed_on": "2026-09-30"},
        }
    )
    return old, new


def ingest(service, records):  # type: ignore[no-untyped-def]
    return service.ingest(
        source_key="sec_edgar",
        candidates=records,
        raw=b"sanitized announcement ordering",
        source_url="https://www.sec.gov/Archives/edgar/data/fixture",
        content_type="application/json",
    )


@pytest.mark.parametrize("newest_first", [True, False])
def test_newer_filing_wins_in_either_batch_order(db_session, fixture_root, tmp_path, newest_first):  # type: ignore[no-untyped-def]
    old, new = announcements(fixture_root)
    service = IngestionService(db_session, tmp_path / "raw")
    result = ingest(service, [new, old] if newest_first else [old, new])
    assert result.state == "HEALTHY"
    current = db_session.scalar(select(EventCurrent))
    assert current and current.event_date.isoformat() == "2026-10-30"
    assert current.source_event_id == "new-filing"
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 1
    assert db_session.scalar(select(func.count()).select_from(SourceAssertion)) == 2
    assertion = db_session.scalar(
        select(SourceAssertion).where(SourceAssertion.source_event_id == "old-filing")
    )
    assert assertion and assertion.facts["source_published_at"] == "2026-09-29T10:00:00Z"
    assert assertion.facts["metadata"]["filed_on"] == "2026-09-29"


def test_old_filing_arriving_later_cannot_refresh_or_roll_back(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    old, new = announcements(fixture_root)
    service = IngestionService(db_session, tmp_path / "raw")
    ingest(service, [new])
    before = db_session.scalar(select(EventCurrent))
    assert before
    observed = before.collected_at
    old.collected_at += timedelta(hours=1)
    result = ingest(service, [old])
    assert result.revised == 0
    current = db_session.scalar(select(EventCurrent))
    assert current and current.event_date.isoformat() == "2026-10-30"
    assert current.collected_at == observed
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 1


@pytest.mark.parametrize("reverse_repeat", [False, True])
def test_same_authority_corroboration_does_not_alternate_revisions(
    db_session, fixture_root, tmp_path, reverse_repeat
):
    original, _ = announcements(fixture_root)
    corroboration = EventCandidate.model_validate(
        {
            **original.model_dump(),
            "source_event_id": "same-filing-exhibit",
            "source_url": "https://www.sec.gov/Archives/edgar/data/fixture/ex99-1.htm",
        }
    )
    service = IngestionService(db_session, tmp_path / "raw")
    first = ingest(service, [original, corroboration])
    assert first.inserted == 1 and first.revised == 0
    current = db_session.scalar(select(EventCurrent))
    assert current.source_event_id == original.source_event_id
    second = ingest(
        service, [corroboration, original] if reverse_repeat else [original, corroboration]
    )
    assert second.inserted == 0 and second.revised == 0 and second.unchanged == 2
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 1
    assert db_session.scalar(select(func.count()).select_from(SourceAssertion)) == 4
    upgraded = EventCandidate.model_validate(
        {**original.model_dump(), "parser_version": "fixture-upgraded"}
    )
    assert ingest(service, [upgraded]).revised == 1
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 2


@pytest.mark.parametrize("clock", ["unknown", "same_day"])
def test_conflicting_unorderable_batch_is_atomic_quarantine(
    db_session, fixture_root, tmp_path, clock
):  # type: ignore[no-untyped-def]
    old, new = announcements(fixture_root)
    for record in (old, new):
        record.source_published_at = None
        record.metadata = {"filed_on": "2026-09-30"} if clock == "same_day" else {}
    result = ingest(IngestionService(db_session, tmp_path / "raw"), [old, new])
    assert result.state == "DEGRADED"
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 0
    assert db_session.scalar(select(func.count()).select_from(EventVersion)) == 0
    assert db_session.scalar(select(func.count()).select_from(SourceAssertion)) == 0
    assert db_session.scalar(select(func.count()).select_from(QuarantineRecord)) == 1
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == 1


def test_older_same_record_revision_cannot_revalidate_conflicting_feed(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    old, new = announcements(fixture_root)
    old.source_event_id = new.source_event_id
    service = IngestionService(db_session, tmp_path / "raw")
    ingest(service, [new])
    publisher = Publisher(db_session, tmp_path / "out")
    publisher.publish()
    pointer = (tmp_path / "out/current.json").read_bytes()
    assert ingest(service, [old]).state == "DEGRADED"
    with pytest.raises(RuntimeError, match="refused"):
        publisher.publish()
    assert (tmp_path / "out/current.json").read_bytes() == pointer


def test_filing_day_without_time_orders_different_days(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    old, new = announcements(fixture_root)
    old.source_published_at = new.source_published_at = None
    ingest(IngestionService(db_session, tmp_path / "raw"), [new, old])
    current = db_session.scalar(select(EventCurrent))
    assert current and current.source_event_id == "new-filing"


def test_unchanged_new_publication_clock_prevents_later_rollback(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    old, new = announcements(fixture_root)
    new.source_event_id = old.source_event_id
    service = IngestionService(db_session, tmp_path / "raw")
    ingest(service, [old])
    fresh = EventCandidate.model_validate(
        {**old.model_dump(), "source_published_at": new.source_published_at}
    )
    assert ingest(service, [fresh]).unchanged == 1
    midway = EventCandidate.model_validate(
        {**new.model_dump(), "source_published_at": "2026-09-29T12:00:00Z"}
    )
    assert ingest(service, [midway]).state == "DEGRADED"
    current = db_session.scalar(select(EventCurrent))
    assert current and current.event_date.isoformat() == "2026-10-29"
