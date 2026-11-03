from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from wtf_ingestion.domain.enums import RunState
from wtf_ingestion.health import aggregate_feed_state, evaluate_run_state
from wtf_ingestion.persistence.models import CollectionRun


def make_run(state: RunState, now: datetime) -> CollectionRun:
    return CollectionRun(
        source_id=uuid4(),
        trigger="test",
        evidence_mode="fixture",
        state=state.value,
        stage="test",
        started_at=now - timedelta(hours=1),
        freshness_deadline=now + timedelta(hours=1),
    )


def test_all_operational_states_are_explicit() -> None:
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert evaluate_run_state(None, now=now) == RunState.NEVER_RUN
    running = make_run(RunState.RUNNING, now)
    assert evaluate_run_state(running, now=now) == RunState.DELAYED
    stale = make_run(RunState.HEALTHY, now)
    stale.freshness_deadline = now - timedelta(seconds=1)
    assert evaluate_run_state(stale, now=now) == RunState.STALE
    assert evaluate_run_state(make_run(RunState.FAILED, now), now=now) == RunState.FAILED
    assert evaluate_run_state(make_run(RunState.VALID_EMPTY, now), now=now) == RunState.VALID_EMPTY
    assert (
        evaluate_run_state(make_run(RunState.SUSPICIOUS_EMPTY, now), now=now)
        == RunState.SUSPICIOUS_EMPTY
    )
    assert aggregate_feed_state([RunState.HEALTHY, RunState.FAILED]) == RunState.DEGRADED
