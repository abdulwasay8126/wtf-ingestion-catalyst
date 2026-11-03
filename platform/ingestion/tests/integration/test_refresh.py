from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from wtf_ingestion.config import Settings
from wtf_ingestion.operations.web import create_app
from wtf_ingestion.persistence.database import Base
from wtf_ingestion.persistence.models import (
    CollectionRun,
    Company,
    OperatorAction,
    RefreshJob,
    RefreshWorker,
    SecCompanyCheckpoint,
    Source,
)
from wtf_ingestion.refresh import RefreshRejected, refresh_status, request_refresh, worker_tick
from wtf_ingestion.registry import seed_registry
from wtf_ingestion.universe import import_universe


def ready(session, tmp_path):  # type: ignore[no-untyped-def]
    source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.permission_status, source.live_enabled, source.fixture_only = "approved", True, False
    session.commit()
    return Settings(
        enable_live_sec=True,
        enable_sec_estimates=True,
        dashboard_mode="live",
        sec_user_agent="Fixture Organization fixture@example.invalid",
        raw_dir=tmp_path / "raw",
        output_dir=tmp_path / "out",
    )


def test_queue_coalesces_idempotent_requests_and_cooldown(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = ready(db_session, tmp_path)
    job = request_refresh(db_session, cfg, idempotency_key="one")
    assert request_refresh(db_session, cfg, idempotency_key="one").id == job.id
    assert request_refresh(db_session, cfg, idempotency_key="two").id == job.id
    assert len(db_session.scalars(select(RefreshJob)).all()) == 1
    assert (
        len(
            db_session.scalars(
                select(OperatorAction).where(OperatorAction.action_type == "refresh")
            ).all()
        )
        == 1
    )
    job.state = "succeeded"
    db_session.commit()
    with pytest.raises(RefreshRejected, match="cooldown"):
        request_refresh(db_session, cfg, idempotency_key="three")


def test_queue_cannot_override_block_or_running_collection(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = ready(db_session, tmp_path)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.permission_status, source.live_enabled = "access_review_required", False
    db_session.commit()
    with pytest.raises(RefreshRejected, match="bypass"):
        request_refresh(db_session, cfg)
    source.permission_status, source.live_enabled = "approved", True
    db_session.add(
        CollectionRun(
            source_id=source.id,
            trigger="manual",
            evidence_mode="live",
            state="RUNNING",
            stage="fetch",
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    with pytest.raises(RefreshRejected, match="already running"):
        request_refresh(db_session, cfg)


def test_worker_failure_is_visible_and_not_auto_retried(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = ready(db_session, tmp_path)
    job = request_refresh(db_session, cfg)
    with patch("wtf_ingestion.refresh.SecCollector") as collector:
        collector.return_value.run.side_effect = RuntimeError("never expose credential headers")
        worker_tick(db_session, cfg)
        assert job.state == "failed" and "credential" not in job.safe_summary
        worker_tick(db_session, cfg)
        assert collector.return_value.run.call_count == 1
    status = refresh_status(db_session)
    assert status["worker_online"] and status["job"]["state"] == "failed"
    assert status["schedule_paused"] and status["next_due_at"] is None
    assert "explicit manual retry" in status["schedule_summary"]


def test_failed_job_allows_only_an_explicit_manual_retry(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = ready(db_session, tmp_path)
    first = request_refresh(db_session, cfg)
    with patch("wtf_ingestion.refresh.SecCollector") as collector:
        collector.return_value.run.side_effect = RuntimeError("diagnostic must not leak")
        worker_tick(db_session, cfg)
        first.requested_at -= timedelta(minutes=16)
        db_session.commit()
        worker_tick(db_session, cfg)
        assert collector.return_value.run.call_count == 1
        retry = request_refresh(db_session, cfg, idempotency_key="reviewed-retry")
        assert retry.id != first.id
        worker_tick(db_session, cfg)
        assert collector.return_value.run.call_count == 2
    assert retry.state == first.state == "failed"


def test_refresh_status_rejects_future_worker_clock_and_blocked_schedule(db_session, tmp_path):  # type: ignore[no-untyped-def]
    ready(db_session, tmp_path)
    db_session.add(
        RefreshWorker(
            key="sec",
            heartbeat_at=datetime.now(UTC) + timedelta(minutes=1),
            next_due_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.live_enabled = False
    source.permission_status = "access_review_required"
    db_session.commit()
    status = refresh_status(db_session)
    assert not status["worker_online"]
    assert status["schedule_paused"] and status["next_due_at"] is None
    assert "cannot re-enable" in status["schedule_summary"]


def test_refresh_status_aggregates_empty_and_mixed_checkpoints(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = ready(db_session, tmp_path)
    job = request_refresh(db_session, cfg)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    now = datetime.now(UTC)
    run = CollectionRun(
        source_id=source.id,
        trigger="manual",
        evidence_mode="live",
        state="RUNNING",
        stage="fetch",
        started_at=now,
        universe_version_id=job.universe_version_id,
    )
    db_session.add(run)
    db_session.flush()
    job.run_id = run.id
    db_session.commit()
    fields = ("review_required", "resumed", "reused_objects", "network_requests", "reused_parses")
    empty = refresh_status(db_session)["job"]
    assert {key: empty[key] for key in fields} == dict.fromkeys(fields, 0)
    assert all(type(empty[key]) is int for key in fields)

    prior_run = CollectionRun(
        source_id=source.id,
        trigger="manual",
        evidence_mode="live",
        state="FAILED",
        stage="fetch",
        started_at=now - timedelta(minutes=30),
        universe_version_id=job.universe_version_id,
    )
    db_session.add(prior_run)
    db_session.flush()
    companies = db_session.scalars(select(Company).order_by(Company.cik).limit(3)).all()
    shared = {
        "universe_version_id": job.universe_version_id,
        "parser_version": "sec-native-v8",
        "config_key": "a" * 64,
        "checked_at": now,
        "saved_at": now,
        "payload": {"candidates": []},
    }
    previous = SecCompanyCheckpoint(
        run_id=prior_run.id,
        company_id=companies[0].id,
        state="VALIDATED",
        reused_objects=99,
        network_requests=99,
        reused_parses=99,
        **shared,
    )
    db_session.add(previous)
    db_session.flush()
    db_session.add_all(
        [
            SecCompanyCheckpoint(
                run_id=run.id,
                company_id=companies[0].id,
                state="VALIDATED",
                reused_from_id=previous.id,
                reused_objects=2,
                network_requests=3,
                reused_parses=4,
                **shared,
            ),
            SecCompanyCheckpoint(
                run_id=run.id,
                company_id=companies[1].id,
                state="REVIEW_REQUIRED",
                reused_objects=5,
                network_requests=6,
                reused_parses=7,
                **shared,
            ),
            SecCompanyCheckpoint(
                run_id=run.id,
                company_id=companies[2].id,
                state="VALIDATED",
                **shared,
            ),
        ]
    )
    db_session.commit()
    actual = refresh_status(db_session)["job"]
    assert [actual[key] for key in fields] == [1, 1, 7, 9, 11]
    assert all(type(actual[key]) is int for key in fields)


@pytest.mark.parametrize("pack_fails", [False, True])
def test_worker_success_updates_progress_and_publication(db_session, tmp_path, pack_fails):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    cfg = ready(db_session, tmp_path)
    job = request_refresh(db_session, cfg)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    run = CollectionRun(
        source_id=source.id,
        trigger="manual",
        evidence_mode="live",
        state="HEALTHY",
        stage="persist",
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.commit()
    with (
        patch("wtf_ingestion.refresh.SecCollector") as collector,
        patch("wtf_ingestion.refresh.Publisher") as publisher,
        patch("wtf_ingestion.refresh.publish_evidence_pack") as build_pack,
    ):

        def collect(**kwargs):  # type: ignore[no-untyped-def]
            progress = collector.call_args.kwargs["progress"]
            progress(run.id, 5, 5, "AAPL")
            return SimpleNamespace(state="HEALTHY")

        collector.return_value.run.side_effect = collect
        # No nonexistent FK: mock publication result has nullable publication identity.
        publisher.return_value.publish.return_value = SimpleNamespace(
            publication_id=None, record_count=5
        )
        if pack_fails:
            build_pack.side_effect = RuntimeError("private diagnostic must never be exposed")
        else:
            build_pack.return_value = {"records": 5}
        worker_tick(db_session, cfg)
    assert job.state == "succeeded" and job.completed_companies == job.total_companies == 5
    assert job.run_id == run.id
    build_pack.assert_called_once()
    assert (
        "unavailable" in job.safe_summary if pack_fails else "pack: 5 records" in job.safe_summary
    )
    assert "private diagnostic" not in job.safe_summary


def test_progress_coalesces_identical_callbacks_without_losing_heartbeat(db_session, tmp_path):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    cfg = ready(db_session, tmp_path)
    job = request_refresh(db_session, cfg)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    run = CollectionRun(
        source_id=source.id,
        trigger="manual",
        evidence_mode="live",
        state="HEALTHY",
        stage="persist",
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.commit()
    with (
        patch("wtf_ingestion.refresh.SecCollector") as collector,
        patch("wtf_ingestion.refresh.Publisher") as publisher,
        patch("wtf_ingestion.refresh.monotonic", return_value=100.0) as tick,
        patch.object(db_session, "commit", wraps=db_session.commit) as commit,
    ):

        def collect(**kwargs):  # type: ignore[no-untyped-def]
            progress = collector.call_args.kwargs["progress"]
            before = commit.call_count
            for _ in range(100):
                progress(run.id, 0, 500, "AAPL")
            assert commit.call_count - before == 1
            tick.return_value += 6.0
            progress(run.id, 0, 500, "AAPL")
            assert commit.call_count - before == 2
            assert refresh_status(db_session)["worker_online"]
            progress(run.id, 1, 500, "AAPL")
            progress(run.id, 1, 500, "MSFT")
            assert commit.call_count - before == 4
            return SimpleNamespace(state="HEALTHY")

        collector.return_value.run.side_effect = collect
        publisher.return_value.publish.return_value = SimpleNamespace(
            publication_id=None, record_count=5
        )
        worker_tick(db_session, cfg)
    assert job.state == "succeeded"
    assert job.completed_companies == 1 and job.current_ticker == "MSFT"


def test_interrupted_job_requires_review(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = ready(db_session, tmp_path)
    job = request_refresh(db_session, cfg)
    job.state = "running"
    db_session.commit()
    with patch("wtf_ingestion.refresh.SecCollector") as collector:
        worker_tick(db_session, cfg)
        assert job.state == "failed" and "interrupted" in job.safe_summary
        collector.assert_not_called()


def test_daily_worker_waits_24h_without_source_requests(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = ready(db_session, tmp_path)
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    now = datetime.now(UTC)
    db_session.add(
        CollectionRun(
            source_id=source.id,
            trigger="manual",
            evidence_mode="live",
            state="HEALTHY",
            stage="persist",
            started_at=now,
            freshness_deadline=now + timedelta(hours=36),
        )
    )
    db_session.commit()
    with patch("wtf_ingestion.refresh.SecCollector") as collector:
        worker_tick(db_session, cfg)
        collector.assert_not_called()
    status = refresh_status(db_session)
    assert datetime.fromisoformat(status["next_due_at"]) == now + timedelta(hours=24)


def test_browser_refresh_rejects_csrf_bad_host_and_fixtures(tmp_path):  # type: ignore[no-untyped-def]
    url = f"sqlite+pysqlite:///{tmp_path / 'web.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_registry(session)
        cfg = ready(session, tmp_path).model_copy(update={"database_url": url})
    with TestClient(create_app(cfg)) as client:
        state = client.get("/refresh/status").json()
        assert state["enabled"]
        assert "worker is offline" in state["disabled_reason"]
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": state["csrf_token"],
            "Idempotency-Key": "browser-one",
        }
        assert client.get("/refresh/status", headers={"Host": "evil.example"}).status_code == 400
        assert client.post("/refresh").status_code == 403
        assert (
            client.post(
                "/refresh", headers={**headers, "Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.post("/refresh", headers={**headers, "X-CSRF-Token": "forged"}).status_code
            == 403
        )
        assert client.post("/refresh", headers=headers).status_code == 202
        assert client.post("/refresh", headers=headers).status_code == 202
    with Session(engine) as session:
        assert len(session.scalars(select(RefreshJob)).all()) == 1
    with TestClient(create_app(Settings(database_url=url))) as client:
        assert client.post("/refresh").status_code == 403
        state = client.get("/refresh/status").json()
        assert not state["enabled"]
        assert "only in the live dashboard" in state["disabled_reason"]
    engine.dispose()


def test_universe_preserves_share_classes_and_scopes_events(db_session, tmp_path):  # type: ignore[no-untyped-def]
    from wtf_ingestion.operations.queries import coverage
    from wtf_ingestion.persistence.models import EventCurrent
    from wtf_ingestion.universe import in_fixed_universe

    rows = [
        {"ticker": ticker, "exchange": "NASDAQ", "company_name": "Alphabet", "cik": "0001652044"}
        for ticker in ("GOOG", "GOOGL")
    ]
    path = tmp_path / "universe.json"
    path.write_text(
        json.dumps(
            {
                "name": "Share classes",
                "version": "classes",
                "effective_from": "2026-10-01",
                "companies": rows,
            }
        )
    )
    import_universe(db_session, path)
    matrix = coverage(db_session)
    assert matrix["total"] == 1 and matrix["listing_total"] == 2
    assert {row["ticker"] for row in matrix["memberships"][0].listings} == {"GOOG", "GOOGL"}
    included = EventCurrent(event_id=uuid4(), event_type="earnings_release", cik="0001652044")
    excluded = EventCurrent(event_id=uuid4(), event_type="earnings_release", cik="0000320193")
    economic = EventCurrent(event_id=uuid4(), event_type="economic_release")
    assert in_fixed_universe(db_session, [included, excluded, economic]) == [included, economic]


def test_successor_requires_review_and_preserves_predecessor(db_session, tmp_path):  # type: ignore[no-untyped-def]
    from wtf_ingestion.persistence.models import Company

    path = tmp_path / "successor.json"
    data = {
        "name": "Reviewed successor",
        "version": "successor-v1",
        "effective_from": "2026-10-01",
        "companies": [
            {"ticker": "XOM", "exchange": "NYSE", "company_name": "Successor", "cik": "0002115436"}
        ],
    }
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="explicit reconciliation"):
        import_universe(db_session, path)
    db_session.rollback()
    data["reconciliations"] = [
        {
            "ticker": "XOM",
            "exchange": "NYSE",
            "previous_cik": "0000034088",
            "current_cik": "0002115436",
            "reviewed_at": "2026-10-01",
            "evidence_url": "https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/d71068d8k12b.htm",
        }
    ]
    path.write_text(json.dumps(data))
    import_universe(db_session, path)
    rows = db_session.scalars(select(Company).where(Company.ticker == "XOM")).all()
    assert {row.cik for row in rows} == {"0000034088", "0002115436"}


def test_running_scan_health_uses_actual_progress():
    from wtf_ingestion.health import evaluate_run_state

    now = datetime.now(UTC)
    run = CollectionRun(
        state="RUNNING",
        started_at=now - timedelta(hours=2),
        last_progress_at=now - timedelta(seconds=10),
    )
    assert evaluate_run_state(run, now=now).value == "RUNNING"
    run.last_progress_at = now - timedelta(hours=1)
    assert evaluate_run_state(run, now=now).value == "DELAYED"
