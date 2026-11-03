from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import select

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.config import Settings
from wtf_ingestion.metadata_watch import check_sec_metadata, validate_metadata
from wtf_ingestion.persistence.locking import PipelineBusyError
from wtf_ingestion.persistence.models import (
    CollectionRun,
    Company,
    EndpointCache,
    RefreshWorker,
    SecMetadataCheck,
    Source,
)
from wtf_ingestion.refresh import worker_tick


def metadata(cik):  # type: ignore[no-untyped-def]
    return json.dumps(
        {
            "cik": cik,
            "filings": {
                "recent": {
                    "accessionNumber": ["0001193125-26-123456"],
                    "filingDate": ["2026-09-29"],
                    "form": ["8-K"],
                    "items": ["2.02"],
                    "primaryDocument": ["report.htm"],
                },
                "files": [],
            },
        }
    ).encode()


def setup_watch(session, tmp_path):  # type: ignore[no-untyped-def]
    source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.permission_status, source.live_enabled, source.fixture_only = "approved", True, False
    cfg = Settings(
        enable_live_sec=True,
        sec_user_agent="Fixture Organization fixture@example.invalid",
        sec_metadata_workers=2,
        sec_metadata_poll_minutes=5,
        raw_dir=tmp_path / "raw",
    )
    cfg.raw_dir.mkdir()
    for company in session.scalars(select(Company)):
        body = metadata(company.cik)
        digest = hashlib.sha256(body).hexdigest()
        path = cfg.raw_dir / f"{digest}.bin"
        path.write_bytes(body)
        url = f"https://data.sec.gov/submissions/CIK{company.cik}.json"
        session.add(
            EndpointCache(
                url_hash=hashlib.sha256(url.encode()).hexdigest(),
                source_id=source.id,
                url=url,
                payload_sha256=digest,
                storage_path=str(path),
                safe_headers={"etag": digest, "content-type": "application/json"},
                validated_at=datetime.now(UTC),
                parser_version="sec-native-v13",
            )
        )
    session.commit()
    return cfg


@respx.mock
def test_unchanged_monitor_is_audited_without_renewing_accepted_cache(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = setup_watch(db_session, tmp_path)
    before = {row.url: row.validated_at for row in db_session.scalars(select(EndpointCache))}
    for company in db_session.scalars(select(Company)):
        route = respx.get(f"https://data.sec.gov/submissions/CIK{company.cik}.json")
        route.mock(return_value=httpx.Response(304))
    check = check_sec_metadata(db_session, cfg)
    assert check.state == "unchanged" and check.payload["checked"] == check.payload["total"] == 5
    assert not check.payload["accepted_evidence_renewed"]
    assert {
        row.url: row.validated_at for row in db_session.scalars(select(EndpointCache))
    } == before
    assert db_session.scalar(select(SecMetadataCheck.id)) == check.id


@respx.mock
def test_changed_metadata_retains_bytes_but_cannot_accept_or_publish(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = setup_watch(db_session, tmp_path)
    for company in db_session.scalars(select(Company)):
        body = metadata(company.cik).replace(b"2.02", b"2.02,9.01")
        respx.get(f"https://data.sec.gov/submissions/CIK{company.cik}.json").mock(
            return_value=httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        )
    check = check_sec_metadata(db_session, cfg)
    assert check.state == "changed" and len(check.payload["changed_ciks"]) == 5
    assert not db_session.scalars(select(CollectionRun)).all()
    for observation in check.payload["observations"]:
        assert (cfg.raw_dir / f"{observation['sha256']}.bin").exists()
        assert observation["sha256"] != observation["baseline_sha256"]


@pytest.mark.parametrize("status", [403, 429])
@respx.mock
def test_access_refusal_disables_monitor_and_preserves_audit(db_session, tmp_path, status):  # type: ignore[no-untyped-def]
    cfg = setup_watch(db_session, tmp_path)
    for company in db_session.scalars(select(Company)):
        respx.get(f"https://data.sec.gov/submissions/CIK{company.cik}.json").mock(
            return_value=httpx.Response(status)
        )
    check = check_sec_metadata(db_session, cfg)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    assert check.state == "failed" and not source.live_enabled
    assert source.permission_status == "access_review_required"
    assert check.payload["http_ledger"]


@pytest.mark.parametrize("case", ["identity", "partial", "empty", "future", "bad_accession"])
def test_metadata_watch_rejects_unsafe_structures(case):
    payload = json.loads(metadata("0000320193"))
    recent = payload["filings"]["recent"]
    if case == "identity":
        payload["cik"] = "0000789019"
    elif case == "partial":
        recent["form"] = []
    elif case == "empty":
        payload["filings"]["recent"] = {key: [] for key in recent}
    elif case == "future":
        recent["filingDate"] = ["2026-11-01"]
    else:
        recent["accessionNumber"] = ["unsafe"]
    with pytest.raises(PayloadError):
        validate_metadata(json.dumps(payload).encode(), "0000320193", datetime.now(UTC))


def test_failed_monitor_does_not_auto_retry_or_start_full_scan(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = setup_watch(db_session, tmp_path)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    db_session.add(
        CollectionRun(
            source_id=source.id,
            trigger="manual",
            evidence_mode="live",
            state="HEALTHY",
            stage="persist",
            started_at=datetime.now(UTC),
        )
    )
    db_session.add(
        RefreshWorker(key="sec", heartbeat_at=datetime.now(UTC), metadata_watch={"state": "failed"})
    )
    db_session.commit()
    with patch("wtf_ingestion.refresh.check_sec_metadata") as watch:
        with patch("wtf_ingestion.refresh.SecCollector") as collector:
            worker_tick(db_session, cfg)
            watch.assert_not_called()
            collector.assert_not_called()


def test_changed_monitor_triggers_one_validated_refresh(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = setup_watch(db_session, tmp_path)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    db_session.add(
        CollectionRun(
            source_id=source.id,
            trigger="manual",
            evidence_mode="live",
            state="HEALTHY",
            stage="persist",
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    check = SimpleNamespace(
        id="test-check",
        state="changed",
        finished_at=datetime.now(UTC),
        payload={"checked": 5, "total": 5, "changed_ciks": ["0000320193"], "error_code": None},
    )
    with (
        patch("wtf_ingestion.refresh.check_sec_metadata", return_value=check) as watch,
        patch("wtf_ingestion.refresh.SecCollector") as collector,
        patch("wtf_ingestion.refresh.Publisher") as publisher,
    ):
        collector.return_value.run.return_value = SimpleNamespace(state="HEALTHY")
        publisher.return_value.publish.return_value = SimpleNamespace(
            publication_id=None, record_count=5
        )
        worker_tick(db_session, cfg)
        watch.assert_called_once()
        collector.return_value.run.assert_called_once_with(trigger="metadata_change")
        publisher.return_value.publish.assert_called_once_with("live")
    worker = db_session.get(RefreshWorker, "sec")
    assert worker.metadata_watch["state"] == "validated_refresh"
    assert worker.metadata_watch["announcement_latency_verified"] is False


def test_monitor_interval_waits_without_network_or_publication(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = setup_watch(db_session, tmp_path)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    db_session.add(
        CollectionRun(
            source_id=source.id,
            trigger="manual",
            evidence_mode="live",
            state="HEALTHY",
            stage="persist",
            started_at=datetime.now(UTC),
        )
    )
    db_session.add(
        RefreshWorker(
            key="sec",
            heartbeat_at=datetime.now(UTC),
            metadata_watch={"state": "unchanged", "finished_at": datetime.now(UTC).isoformat()},
        )
    )
    db_session.commit()
    with (
        patch("wtf_ingestion.refresh.check_sec_metadata") as watch,
        patch("wtf_ingestion.refresh.SecCollector") as collector,
    ):
        worker_tick(db_session, cfg)
        watch.assert_not_called()
        collector.assert_not_called()


@pytest.mark.parametrize("busy", [True, False])
def test_watch_lease_collision_defers_and_preflight_failure_pauses(db_session, tmp_path, busy):  # type: ignore[no-untyped-def]
    cfg = setup_watch(db_session, tmp_path)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    db_session.add(
        CollectionRun(
            source_id=source.id,
            trigger="manual",
            evidence_mode="live",
            state="HEALTHY",
            stage="persist",
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    before = {row.url: row.validated_at for row in db_session.scalars(select(EndpointCache))}
    error = PipelineBusyError("busy") if busy else PayloadError("invalid universe")
    with (
        patch("wtf_ingestion.refresh.check_sec_metadata", side_effect=error),
        patch("wtf_ingestion.refresh.SecCollector") as collector,
    ):
        worker_tick(db_session, cfg)
        collector.assert_not_called()
    worker = db_session.get(RefreshWorker, "sec")
    assert worker.metadata_watch["state"] == ("deferred" if busy else "failed")
    assert not db_session.scalars(select(SecMetadataCheck)).all()
    assert {
        row.url: row.validated_at for row in db_session.scalars(select(EndpointCache))
    } == before
