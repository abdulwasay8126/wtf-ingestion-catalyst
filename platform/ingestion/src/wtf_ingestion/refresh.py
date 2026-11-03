from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.base import SourceError
from wtf_ingestion.collectors import SecCollector
from wtf_ingestion.config import Settings
from wtf_ingestion.earnings_evidence import publish_evidence_pack
from wtf_ingestion.health import as_utc
from wtf_ingestion.metadata_watch import check_sec_metadata
from wtf_ingestion.persistence.locking import PipelineBusyError, pipeline_lock
from wtf_ingestion.persistence.models import (
    CollectionRun,
    OperatorAction,
    RefreshJob,
    RefreshWorker,
    SecCompanyCheckpoint,
    Source,
)
from wtf_ingestion.publication.publisher import Publisher
from wtf_ingestion.universe import active_universe

QUEUE_LOCK = 8621902
WORKER_LOCK = 8621903
COOLDOWN = timedelta(minutes=15)
PROGRESS_HEARTBEAT_SECONDS = 5.0


class RefreshRejected(RuntimeError):
    pass


def request_refresh(
    session: Session,
    cfg: Settings,
    *,
    trigger: str = "manual",
    idempotency_key: str | None = None,
    actor: str | None = None,
) -> RefreshJob:
    cfg.assert_live_sec_ready()
    with pipeline_lock(session, QUEUE_LOCK):
        source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
        if (
            not source
            or not source.live_enabled
            or source.fixture_only
            or source.permission_status != "approved"
        ):
            raise RefreshRejected(
                "SEC access is disabled or needs source review; refresh cannot bypass it."
            )
        universe = active_universe(session)
        if universe is None:
            raise RefreshRejected("Import a reviewed fixed universe before refreshing.")
        if idempotency_key:
            action = session.scalar(
                select(OperatorAction).where(OperatorAction.idempotency_key == idempotency_key)
            )
            if action:
                job = session.scalar(select(RefreshJob).where(RefreshJob.action_id == action.id))
                if job:
                    return job
                raise RefreshRejected("Idempotency key was used for another action.")
        pending = session.scalar(
            select(RefreshJob).where(RefreshJob.state.in_(["queued", "running"]))
        )
        if pending:
            return pending
        running = session.scalar(select(CollectionRun.id).where(CollectionRun.state == "RUNNING"))
        if running:
            raise RefreshRejected("A collection is already running; no second request was started.")
        latest = session.scalar(
            select(RefreshJob).order_by(RefreshJob.requested_at.desc()).limit(1)
        )
        now = datetime.now(UTC)
        if trigger == "manual" and latest and now - as_utc(latest.requested_at) < COOLDOWN:
            raise RefreshRejected("Refresh cooldown: wait 15 minutes after the previous request.")
        action = OperatorAction(
            action_type="refresh",
            actor=actor or ("local-dashboard" if trigger == "manual" else "local-worker"),
            target_id="sec_edgar",
            reason=f"{trigger} fixed-universe collection and validated publication",
            idempotency_key=idempotency_key or f"refresh-{uuid4()}",
        )
        session.add(action)
        session.flush()
        job = RefreshJob(
            action_id=action.id,
            universe_version_id=universe.id,
            trigger=trigger,
            requested_at=now,
            safe_summary="Queued; waiting for the local refresh worker.",
        )
        session.add(job)
        session.commit()
        return job


def refresh_status(session: Session) -> dict[str, Any]:
    worker = session.get(RefreshWorker, "sec")
    job = session.scalar(select(RefreshJob).order_by(RefreshJob.requested_at.desc()).limit(1))
    source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    latest_run = session.scalar(
        select(CollectionRun)
        .where(CollectionRun.evidence_mode == "live")
        .order_by(CollectionRun.started_at.desc())
        .limit(1)
    )
    now = datetime.now(UTC)
    metrics = (
        session.execute(
            select(
                func.coalesce(
                    func.sum(case((SecCompanyCheckpoint.state == "REVIEW_REQUIRED", 1), else_=0)),
                    0,
                ).label("review_required"),
                func.coalesce(
                    func.sum(case((SecCompanyCheckpoint.reused_from_id.is_not(None), 1), else_=0)),
                    0,
                ).label("resumed"),
                func.coalesce(func.sum(SecCompanyCheckpoint.reused_objects), 0).label(
                    "reused_objects"
                ),
                func.coalesce(func.sum(SecCompanyCheckpoint.network_requests), 0).label(
                    "network_requests"
                ),
                func.coalesce(func.sum(SecCompanyCheckpoint.reused_parses), 0).label(
                    "reused_parses"
                ),
            ).where(SecCompanyCheckpoint.run_id == job.run_id)
        ).one()
        if job and job.run_id
        else None
    )
    heartbeat = max(
        [
            as_utc(value)
            for value in (
                worker.heartbeat_at if worker else None,
                job.heartbeat_at if job and job.state == "running" else None,
            )
            if value is not None
        ],
        default=None,
    )
    worker_online = bool(heartbeat and timedelta(0) <= now - heartbeat < timedelta(minutes=5))
    source_ready = bool(
        source
        and source.live_enabled
        and not source.fixture_only
        and source.permission_status == "approved"
    )
    watch = worker.metadata_watch if worker else {}
    schedule_paused = not source_ready or bool(
        watch.get("state") == "failed"
        or (job and job.state == "failed")
        or (latest_run and latest_run.state not in {"HEALTHY", "VALID_EMPTY", "RUNNING"})
    )
    schedule_summary = (
        "Daily refresh paused: SEC access requires review; refresh cannot re-enable it."
        if not source_ready
        else "Daily refresh paused after failure. Review Runs; explicit manual retry is required."
        if schedule_paused
        else "SEC metadata checks trigger validated refreshes; daily full checks remain scheduled."
        if watch.get("interval_minutes")
        else "Daily source refresh is scheduled; viewing this page does not collect data."
    )
    return {
        "cooldown_seconds": max(0, int((as_utc(job.requested_at) + COOLDOWN - now).total_seconds()))
        if job
        else 0,
        "worker_online": worker_online,
        "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
        "next_due_at": as_utc(worker.next_due_at).isoformat()
        if worker and worker.next_due_at and not schedule_paused
        else None,
        "schedule_paused": schedule_paused,
        "schedule_summary": schedule_summary,
        "metadata_watch": watch,
        "job": {
            "id": str(job.id),
            "state": job.state,
            "summary": job.safe_summary,
            "completed": job.completed_companies,
            "total": job.total_companies,
            "ticker": job.current_ticker,
            "publication_id": str(job.publication_id) if job.publication_id else None,
            "requested_at": as_utc(job.requested_at).isoformat(),
            "review_required": int(metrics.review_required) if metrics else 0,
            "resumed": int(metrics.resumed) if metrics else 0,
            "reused_objects": int(metrics.reused_objects) if metrics else 0,
            "network_requests": int(metrics.network_requests) if metrics else 0,
            "reused_parses": int(metrics.reused_parses) if metrics else 0,
        }
        if job
        else None,
    }


def worker_tick(
    session: Session, cfg: Settings, interval_hours: int = 24, *, force: bool = False
) -> None:
    """Called only by the single worker holding WORKER_LOCK; no auto-retry of failed jobs."""
    now = datetime.now(UTC)
    worker = session.get(RefreshWorker, "sec")
    if worker is None:
        worker = RefreshWorker(key="sec", heartbeat_at=now)
        session.add(worker)
    worker.heartbeat_at = now
    source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    latest_run = session.scalar(
        select(CollectionRun)
        .where(CollectionRun.evidence_mode == "live")
        .order_by(CollectionRun.started_at.desc())
        .limit(1)
    )
    latest_job = session.scalar(
        select(RefreshJob).order_by(RefreshJob.requested_at.desc()).limit(1)
    )
    # The exclusive worker lease proves any unfinished job is interrupted, not another worker's run.
    if latest_job and latest_job.state == "running":
        latest_job.state = "failed"
        latest_job.finished_at = now
        latest_job.safe_summary = (
            "Worker interrupted. Review run/raw evidence before an explicit retry."
        )
        if latest_job.run_id:
            interrupted = session.get(CollectionRun, latest_job.run_id)
            if interrupted and interrupted.state == "RUNNING":
                interrupted.state = "FAILED"
                interrupted.finished_at = now
                interrupted.safe_error_code = "WORKER_INTERRUPTED"
                interrupted.safe_error_summary = latest_job.safe_summary
    anchor = as_utc(latest_run.started_at) if latest_run else now
    if latest_job:
        anchor = max(anchor, as_utc(latest_job.requested_at))
    worker.next_due_at = anchor + timedelta(hours=interval_hours)
    session.commit()
    job = session.scalar(
        select(RefreshJob)
        .where(RefreshJob.state == "queued")
        .order_by(RefreshJob.requested_at)
        .limit(1)
    )
    healthy = latest_run is None or latest_run.state in {"HEALTHY", "VALID_EMPTY"}
    job_ok = latest_job is None or latest_job.state != "failed"
    watch = worker.metadata_watch or {}
    watch_ok = watch.get("state") != "failed"
    if (
        not job
        and cfg.sec_metadata_poll_minutes
        and source
        and source.live_enabled
        and not source.fixture_only
        and source.permission_status == "approved"
        and healthy
        and job_ok
        and watch_ok
        and latest_run is not None
    ):
        last_check = watch.get("finished_at")
        due = (
            datetime.fromisoformat(last_check) + timedelta(minutes=cfg.sec_metadata_poll_minutes)
            if last_check
            else now
        )
        if now >= due:
            try:
                check = check_sec_metadata(session, cfg)
            except PipelineBusyError:
                worker.metadata_watch = {
                    **watch,
                    "state": "deferred",
                    "error_code": "PIPELINE_BUSY",
                    "deferred_at": now.isoformat(),
                }
                session.commit()
                return
            except (SourceError, RuntimeError) as exc:
                worker.metadata_watch = {
                    **watch,
                    "state": "failed",
                    "error_code": getattr(exc, "code", "METADATA_PREFLIGHT_ERROR"),
                    "finished_at": now.isoformat(),
                    "announcement_latency_verified": False,
                }
                session.commit()
                return
            worker.heartbeat_at = check.finished_at
            worker.metadata_watch = {
                "state": check.state,
                "check_id": str(check.id),
                "finished_at": as_utc(check.finished_at).isoformat(),
                "checked": check.payload["checked"],
                "total": check.payload["total"],
                "changed_count": len(check.payload["changed_ciks"]),
                "error_code": check.payload["error_code"],
                "interval_minutes": cfg.sec_metadata_poll_minutes,
                "target_minutes": cfg.earnings_update_target_minutes,
                "announcement_latency_verified": False,
            }
            session.commit()
            watch_ok = check.state != "failed"
            if check.state == "changed":
                job = request_refresh(session, cfg, trigger="metadata_change")
    if (
        not job
        and source
        and source.live_enabled
        and source.permission_status == "approved"
        and (
            force
            or (
                healthy
                and job_ok
                and watch_ok
                and (latest_run is None or now >= as_utc(worker.next_due_at))
            )
        )
    ):
        job = request_refresh(session, cfg, trigger="scheduled")
    if job is None:
        return
    with pipeline_lock(session, QUEUE_LOCK):
        job.state = "running"
        job.started_at = now
        job.heartbeat_at = now
        job.safe_summary = (
            "Collecting source evidence; publication will wait for complete validation."
        )
        session.commit()

    last_progress_key: tuple[UUID, int, int, str] | None = None
    last_progress_write = 0.0

    def progress(run_id: UUID, completed: int, total: int, ticker: str) -> None:
        nonlocal last_progress_key, last_progress_write
        key = (run_id, completed, total, ticker)
        tick = monotonic()
        # Raw capture and checkpoints commit independently. Do not write identical
        # UI progress for every cached document; keep ticker/count changes immediate.
        if key == last_progress_key and tick - last_progress_write < PROGRESS_HEARTBEAT_SECONDS:
            return
        job.run_id = run_id
        job.completed_companies = completed
        job.total_companies = total
        job.current_ticker = ticker
        job.heartbeat_at = datetime.now(UTC)
        worker.heartbeat_at = datetime.now(UTC)
        run = session.get(CollectionRun, run_id)
        reviews = (
            sum(flag.startswith("company_review:") for flag in run.anomaly_flags) if run else 0
        )
        job.safe_summary = (
            f"{reviews} company reviews pending. Saved checks are resumable; "
            "partial preview is separate from the unpublished full feed."
        )
        session.commit()
        last_progress_key = key
        last_progress_write = monotonic()

    try:
        universe = active_universe(session)
        if universe is None or universe.id != job.universe_version_id:
            raise RefreshRejected(
                "Universe changed while queued; request a fresh job for the selected scope."
            )
        result = SecCollector(cfg, session, progress=progress).run(trigger=job.trigger)
        if result.state not in {"HEALTHY", "VALID_EMPTY"}:
            raise RefreshRejected(
                "Collection did not pass acceptance checks; last-known-good feed preserved."
            )
        job.safe_summary = "Collection validated; publishing the new snapshot."
        job.heartbeat_at = datetime.now(UTC)
        session.commit()
        publication = Publisher(session, cfg.output_dir, cfg.publication_horizon_days).publish(
            "live"
        )
        job.publication_id = publication.publication_id
        job.state = "succeeded"
        job.safe_summary = (
            f"Published {publication.record_count} validated records. Missing dates remain missing."
        )
        if worker.metadata_watch:
            worker.metadata_watch = {**worker.metadata_watch, "state": "validated_refresh"}
        # Calendar publication already succeeded. Research failures must remain
        # explicit without relabelling accepted source facts as a failed refresh.
        session.commit()
        try:
            pack = publish_evidence_pack(session, cfg)
            job.safe_summary += f" Source-verified evidence pack: {pack['records']} records."
        except Exception:
            session.rollback()
            job.safe_summary += (
                " Research evidence pack unavailable; review retained inputs "
                "and rebuild explicitly."
            )
    except Exception as exc:
        session.rollback()
        job.state = "failed"
        job.safe_summary = (
            f"Refresh failed ({getattr(exc, 'code', exc.__class__.__name__)}). "
            "Review Runs and Sources; old feed is not current."
        )
        if getattr(exc, "code", "") == "SEC_SCAN_INCOMPLETE":
            reviews = session.scalar(
                select(func.count())
                .select_from(SecCompanyCheckpoint)
                .where(
                    SecCompanyCheckpoint.run_id == job.run_id,
                    SecCompanyCheckpoint.state == "REVIEW_REQUIRED",
                )
            )
            job.safe_summary = (
                f"Checked {job.completed_companies}/{job.total_companies} companies; "
                f"{reviews} need evidence review. Partial preview available; "
                "official feed withheld. Validated progress is saved for retry."
            )
    finally:
        job.finished_at = datetime.now(UTC)
        job.heartbeat_at = job.finished_at
        worker.heartbeat_at = job.finished_at
        session.commit()
