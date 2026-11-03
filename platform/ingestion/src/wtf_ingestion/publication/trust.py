from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.domain.enums import RunState
from wtf_ingestion.health import aggregate_feed_state, as_utc, evaluate_run_state
from wtf_ingestion.persistence.models import (
    CollectionRun,
    PublicationManifest,
    RefreshJob,
    RefreshWorker,
)
from wtf_ingestion.publication.files import replace_atomic
from wtf_ingestion.universe import active_universe


def publication_trust(session: Session, publication: PublicationManifest | None) -> str:
    if publication is None:
        return RunState.NEVER_RUN.value
    universe = active_universe(session)
    if universe and publication.universe_version_id != universe.id:
        return RunState.DEGRADED.value
    worker = session.get(RefreshWorker, "sec") if publication.evidence_mode == "live" else None
    if worker and (worker.metadata_watch or {}).get("state") == "failed":
        return RunState.DEGRADED.value
    failed_refresh = session.scalar(
        select(RefreshJob.id)
        .where(RefreshJob.state == "failed", RefreshJob.finished_at > publication.generated_at)
        .limit(1)
    )
    if failed_refresh:
        return RunState.DEGRADED.value
    runs = session.scalars(
        select(CollectionRun)
        .where(
            CollectionRun.evidence_mode == publication.evidence_mode,
        )
        .order_by(CollectionRun.started_at.desc())
    ).all()
    latest: dict[UUID, CollectionRun] = {}
    for run in runs:
        latest.setdefault(run.source_id, run)
    state = aggregate_feed_state([evaluate_run_state(run) for run in latest.values()])
    if state != RunState.HEALTHY:
        return state.value
    pending = session.scalar(
        select(PublicationManifest.id)
        .where(
            PublicationManifest.activated_at.is_(None),
            PublicationManifest.generated_at > publication.generated_at,
        )
        .limit(1)
    )
    if pending:
        return RunState.DEGRADED.value
    if publication.freshness_deadline is None or as_utc(
        publication.freshness_deadline
    ) <= datetime.now(UTC):
        return RunState.STALE.value
    return state.value


def write_status(session: Session, output_dir: Path) -> None:
    publication = session.scalar(
        select(PublicationManifest)
        .where(
            PublicationManifest.activated_at.is_not(None),
        )
        .order_by(PublicationManifest.generated_at.desc())
        .limit(1)
    )
    state = publication_trust(session, publication)
    payload = {
        "checked_at": datetime.now(UTC).isoformat(),
        "publication_id": str(publication.id) if publication else None,
        "state": state,
        "is_current": state == RunState.HEALTHY.value,
        "evidence_mode": publication.evidence_mode if publication else None,
        "freshness_deadline": as_utc(publication.freshness_deadline).isoformat()
        if publication and publication.freshness_deadline
        else None,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "status.json"
    temporary = path.with_name(f".status.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    replace_atomic(temporary, path)
