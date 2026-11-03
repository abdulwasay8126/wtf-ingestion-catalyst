from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings
from wtf_ingestion.health import as_utc
from wtf_ingestion.persistence.models import CollectionRun, PublicationManifest
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher


def test_pointer_failure_is_recoverable_only_after_checksum_validation(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    service = IngestionService(db_session, tmp_path / "raw")
    service.ingest(
        source_key="sec_edgar",
        candidates=SecEarningsAdapter(Settings()).parse_fixture(raw, datetime.now(UTC)),
        raw=raw,
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    publisher = Publisher(db_session, tmp_path / "out")
    first = publisher.publish()
    before = (tmp_path / "out/current.json").read_bytes()
    original = publisher._write_text_atomic

    def failing_write(path, content):  # type: ignore[no-untyped-def]
        if path.name == "current.json":
            raise OSError("simulated pointer replacement failure")
        original(path, content)

    with patch.object(publisher, "_write_text_atomic", failing_write), pytest.raises(OSError):
        publisher.publish()
    assert (tmp_path / "out/current.json").read_bytes() == before
    pending = db_session.scalar(
        select(PublicationManifest).where(PublicationManifest.activated_at.is_(None))
    )
    assert pending and pending.id != first.publication_id
    publisher.recover_pointer(pending.id)
    assert pending.activated_at is not None


def test_reobserved_unchanged_event_does_not_inherit_old_revision_freshness(
    db_session, fixture_root, tmp_path
):  # type: ignore[no-untyped-def]
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    service = IngestionService(db_session, tmp_path / "raw")
    candidate = SecEarningsAdapter(Settings()).parse_fixture(raw, datetime.now(UTC))[0]
    args = dict(
        source_key="sec_edgar",
        candidates=[candidate],
        raw=raw,
        source_url="https://data.sec.gov/test",
        content_type="application/json",
    )
    first = service.ingest(**args)
    run = db_session.get(CollectionRun, first.run_id)
    run.freshness_deadline = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    service.ingest(**args)
    result = Publisher(db_session, tmp_path / "out").publish()
    publication = db_session.get(PublicationManifest, result.publication_id)
    assert publication and publication.freshness_deadline
    assert as_utc(publication.freshness_deadline) > datetime.now(UTC)
