from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wtf_ingestion.domain.enums import RunState
from wtf_ingestion.persistence.models import CollectionRun


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def evaluate_run_state(
    run: CollectionRun | None,
    *,
    now: datetime | None = None,
    running_grace: timedelta = timedelta(minutes=30),
) -> RunState:
    if run is None:
        return RunState.NEVER_RUN
    observed = now or datetime.now(UTC)
    if run.state == RunState.RUNNING.value:
        return (
            RunState.DELAYED
            if observed - as_utc(run.last_progress_at or run.started_at) > running_grace
            else RunState.RUNNING
        )
    if run.state == RunState.SUSPICIOUS_EMPTY.value:
        return RunState.SUSPICIOUS_EMPTY
    if run.state == RunState.FAILED.value:
        return RunState.FAILED
    if run.state == RunState.DEGRADED.value:
        return RunState.DEGRADED
    if run.freshness_deadline and as_utc(run.freshness_deadline) < observed:
        return RunState.STALE
    if run.state == RunState.VALID_EMPTY.value:
        return RunState.VALID_EMPTY
    return RunState.HEALTHY


def aggregate_feed_state(states: list[RunState]) -> RunState:
    if not states or all(state == RunState.NEVER_RUN for state in states):
        return RunState.NEVER_RUN
    if all(state == RunState.FAILED for state in states):
        return RunState.FAILED
    if any(state == RunState.STALE for state in states):
        return RunState.STALE
    if any(
        state in {RunState.FAILED, RunState.SUSPICIOUS_EMPTY, RunState.NEVER_RUN, RunState.DEGRADED}
        for state in states
    ):
        return RunState.DEGRADED
    if any(state == RunState.DELAYED for state in states):
        return RunState.DELAYED
    if any(state == RunState.RUNNING for state in states):
        return RunState.RUNNING
    return RunState.HEALTHY
