from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from wtf_ingestion.domain.models import EventCandidate
from wtf_ingestion.health import as_utc, evaluate_run_state
from wtf_ingestion.persistence.models import (
    CollectionRun,
    Company,
    EventCurrent,
    EventVersion,
    PublicationManifest,
    SecCompanyCheckpoint,
    Source,
    SourceAssertion,
    UniverseMembership,
    UniverseVersion,
)
from wtf_ingestion.publication.trust import publication_trust
from wtf_ingestion.selection import without_shadowed_projections
from wtf_ingestion.universe import in_fixed_universe


def sources(session: Session, *, live: bool) -> dict[str, Any]:
    """Present real earnings routes separately from the isolated fixture registry."""
    registered = session.scalars(select(Source).order_by(Source.name)).all()
    visible = (
        [
            source
            for source in registered
            if source.key == "sec_edgar"
            or (source.source_type == "issuer_ir" and not source.fixture_only)
        ]
        if live
        else registered
    )
    query = select(CollectionRun).where(CollectionRun.source_id.in_([row.id for row in visible]))
    if live:
        query = query.where(CollectionRun.evidence_mode == "live")
    latest: dict[UUID, CollectionRun] = {}
    for run in session.scalars(query.order_by(CollectionRun.started_at.desc())):
        latest.setdefault(run.source_id, run)
    rows: list[dict[str, Any]] = [
        {
            "source": source,
            "run": latest.get(source.id),
            "state": evaluate_run_state(latest.get(source.id)).value,
            "collecting": bool(
                source.live_enabled
                and not source.fixture_only
                and source.permission_status == "approved"
            ),
        }
        for source in visible
    ]
    return {
        "source_rows": rows,
        "sec_sources": [row for row in rows if row["source"].key == "sec_edgar"],
        "issuer_sources": [row for row in rows if row["source"].source_type == "issuer_ir"],
    }


def scan_preview(session: Session) -> dict[str, Any]:
    """Staged source checks only. Never used by Publisher or events_feed_current."""
    from wtf_ingestion.universe import active_universe

    universe = active_universe(session)
    source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    run = (
        session.scalar(
            select(CollectionRun)
            .where(
                CollectionRun.source_id == source.id,
                CollectionRun.universe_version_id == universe.id,
            )
            .order_by(CollectionRun.started_at.desc())
            .limit(1)
        )
        if source and universe
        else None
    )
    checkpoints = (
        session.scalars(
            select(SecCompanyCheckpoint)
            .where(SecCompanyCheckpoint.run_id == run.id)
            .order_by(SecCompanyCheckpoint.saved_at)
        ).all()
        if run
        else []
    )
    companies = {row.id: row for row in session.scalars(select(Company))} if checkpoints else {}
    now = datetime.now(UTC)
    candidates: list[EventCandidate] = []
    retained_candidates: list[EventCandidate] = []
    reviews = []
    stale = 0
    for checkpoint in checkpoints:
        company = companies[checkpoint.company_id]
        if checkpoint.state != "VALIDATED":
            reviews.append(
                {
                    "ticker": company.ticker,
                    "code": checkpoint.error_code,
                    "summary": checkpoint.safe_summary,
                    "url": checkpoint.payload.get("issue_url"),
                }
            )
            continue
        expired = not timedelta(0) <= now - as_utc(checkpoint.checked_at) < timedelta(hours=6)
        if expired:
            stale += 1
        for values in checkpoint.payload.get("candidates", []):
            candidate = EventCandidate.model_validate(values)
            if (
                candidate.event_date
                and now.date() <= candidate.event_date <= now.date() + timedelta(days=90)
            ):
                (retained_candidates if expired else candidates).append(candidate)
    candidates.sort(key=lambda row: (row.event_date or now.date(), row.ticker or ""))
    retained_candidates.sort(key=lambda row: (row.event_date or now.date(), row.ticker or ""))
    return {
        "run": run,
        "checked": len(checkpoints),
        "validated": len(checkpoints) - len(reviews),
        "reviews": reviews,
        "stale": stale,
        "candidates": candidates[:1000],
        "retained_candidates": retained_candidates[:1000],
        "network_requests": sum(row.network_requests for row in checkpoints),
        "reused_objects": sum(row.reused_objects for row in checkpoints),
        "reused_parses": sum(row.reused_parses for row in checkpoints),
        "resumed": sum(row.reused_from_id is not None for row in checkpoints),
        "source_enabled": bool(
            source and source.live_enabled and source.permission_status == "approved"
        ),
        "checkpoints": {row.company_id: row for row in checkpoints},
    }


def overview(session: Session, *, live: bool = False) -> dict[str, Any]:
    latest_publication = session.scalar(
        select(PublicationManifest)
        .where(PublicationManifest.activated_at.is_not(None))
        .order_by(PublicationManifest.generated_at.desc())
        .limit(1)
    )
    events = session.scalars(
        select(EventCurrent)
        .where(
            EventCurrent.event_date >= datetime.now(UTC).date(),
            EventCurrent.event_date <= datetime.now(UTC).date() + timedelta(days=90),
        )
        .order_by(EventCurrent.event_date, EventCurrent.ticker)
    ).all()
    events = without_shadowed_projections(in_fixed_universe(session, events))[:12]
    source_rows = sources(session, live=live)["source_rows"]
    coverage_data = coverage(session)
    membership_total = coverage_data["total"]
    covered = sum(
        coverage_data["counts"].get(key, 0) for key in ("confirmed", "announced_date_only")
    )
    return {
        "publication": latest_publication,
        "feed_state": publication_trust(session, latest_publication),
        "events": events,
        "sources": source_rows,
        "universe_total": membership_total,
        "covered": covered,
        "coverage_percent": round((covered / membership_total * 100), 1) if membership_total else 0,
        "estimated_count": coverage_data["counts"].get("estimated", 0),
        "scan_preview": coverage_data["scan_preview"],
    }


def event_detail(session: Session, event_id: UUID) -> dict[str, Any] | None:
    event = session.get(EventCurrent, event_id)
    if event is None:
        return None
    versions = session.scalars(
        select(EventVersion)
        .where(EventVersion.event_id == event_id)
        .order_by(EventVersion.revision_number.desc())
    ).all()
    assertions = session.scalars(
        select(SourceAssertion)
        .where(SourceAssertion.event_id == event_id)
        .order_by(SourceAssertion.observed_at.desc())
    ).all()
    assertion_modes = {}
    for assertion in assertions:
        run = session.get(CollectionRun, assertion.run_id)
        assertion_modes[assertion.id] = run.evidence_mode if run else "unknown"
    return {
        "event": event,
        "versions": versions,
        "assertions": assertions,
        "assertion_modes": assertion_modes,
    }


@dataclass(frozen=True)
class CoverageRow:
    company: Company
    coverage_state: str
    state_reason: str
    listings: list[dict[str, str]]


def coverage(session: Session) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    universe = session.scalar(
        select(UniverseVersion)
        .where(
            UniverseVersion.effective_from <= today,
            (UniverseVersion.effective_to.is_(None) | (UniverseVersion.effective_to >= today)),
        )
        .order_by(UniverseVersion.effective_from.desc(), UniverseVersion.created_at.desc())
        .limit(1)
    )
    if universe is None:
        return {
            "memberships": [],
            "counts": {},
            "total": 0,
            "universe": None,
            "scan_preview": scan_preview(session),
        }
    memberships = session.scalars(
        select(UniverseMembership)
        .options(selectinload(UniverseMembership.company))
        .where(UniverseMembership.universe_version_id == universe.id)
    ).all()
    all_events = session.scalars(
        select(EventCurrent).where(
            EventCurrent.event_type == "earnings_release",
            EventCurrent.event_date >= today,
            EventCurrent.event_date <= today + timedelta(days=90),
            EventCurrent.status.in_(["confirmed", "moved", "cancelled", "estimated"]),
        )
    ).all()
    by_cik: dict[str | None, list[EventCurrent]] = {}
    for event in all_events:
        by_cik.setdefault(event.cik, []).append(event)
    latest_runs: dict[UUID, CollectionRun] = {}
    for run in session.scalars(select(CollectionRun).order_by(CollectionRun.started_at.desc())):
        latest_runs.setdefault(run.source_id, run)
    scope_checked = any(
        run.universe_version_id == universe.id and run.state in {"HEALTHY", "VALID_EMPTY"}
        for run in latest_runs.values()
    )
    preview = scan_preview(session)
    rows = []
    for membership in memberships:
        events = by_cik.get(membership.company.cik, [])
        events = [row for row in without_shadowed_projections(events) if row.status != "cancelled"]
        state = "not_yet_announced"
        reason = "No accepted announcement in the 90-day window; no date is inferred."
        if not scope_checked and universe.provenance:
            state = "not_scanned"
            reason = "No complete successful scan of this universe yet; date coverage is unknown."
        if events:
            state = "source_unavailable"
            reason = "Accepted evidence exists, but its source or observation is not fresh."
            for event in sorted(
                events, key=lambda item: (item.status == "estimated", item.event_date)
            ):
                assertion = session.scalar(
                    select(SourceAssertion)
                    .join(CollectionRun, CollectionRun.id == SourceAssertion.run_id)
                    .where(
                        SourceAssertion.event_id == event.event_id,
                        SourceAssertion.source_id == event.source_id,
                        SourceAssertion.source_event_id == event.source_event_id,
                    )
                    .order_by(CollectionRun.started_at.desc(), SourceAssertion.observed_at.desc())
                    .limit(1)
                )
                latest = latest_runs.get(event.source_id)
                observation = session.get(CollectionRun, assertion.run_id) if assertion else None
                version = session.get(EventVersion, event.current_version_id)
                facts_match = bool(
                    assertion
                    and version
                    and all(
                        assertion.facts.get(key) == value
                        for key, value in EventCandidate.model_validate(version.payload)
                        .material_dict()
                        .items()
                    )
                )
                if (
                    evaluate_run_state(latest).value
                    in {
                        "HEALTHY",
                        "VALID_EMPTY",
                    }
                    and evaluate_run_state(observation).value in {"HEALTHY", "VALID_EMPTY"}
                    and facts_match
                    and (not universe.provenance or scope_checked)
                ):
                    state = (
                        "estimated"
                        if event.status == "estimated"
                        else (
                            "announced_date_only"
                            if event.time_precision == "date_only"
                            else "confirmed"
                        )
                    )
                    evidence = observation.evidence_mode if observation else "unknown"
                    reason = (
                        f"Fresh {evidence} low-confidence SEC filing-cadence estimate; "
                        "not announced."
                        if event.status == "estimated"
                        else f"Fresh {evidence} evidence from {event.source_key}."
                    )
                    break
        checkpoint = preview["checkpoints"].get(membership.company_id)
        if not scope_checked and checkpoint:
            if checkpoint.state != "VALIDATED":
                state = "review_required"
                reason = (
                    f"Partial scan: {checkpoint.error_code}. No date accepted for this company."
                )
            elif datetime.now(UTC) - as_utc(checkpoint.checked_at) >= timedelta(hours=6):
                state = "preview_expired"
                reason = "Saved partial check expired; revalidation required. Not a current feed."
            else:
                state = "checked_partial"
                reason = (
                    "Source evidence checked; see staged candidates below. Full feed not published."
                )
        rows.append(CoverageRow(membership.company, state, reason, membership.listings))
    counts = Counter(item.coverage_state for item in rows)
    return {
        "memberships": rows,
        "counts": dict(counts),
        "total": len(rows),
        "universe": universe,
        "listing_total": sum(len(row.listings) or 1 for row in rows),
        "scan_preview": preview,
    }
