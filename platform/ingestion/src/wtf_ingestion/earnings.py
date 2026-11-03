"""Publication-bound earnings facts and explicit information gaps.

The consumer reads the immutable accepted feed, never unpublished current rows.
It performs no source requests or narrative generation on GET.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, selectinload

from wtf_ingestion.config import Settings
from wtf_ingestion.domain.models import EventCandidate, FeedRecord
from wtf_ingestion.health import as_utc
from wtf_ingestion.persistence.models import (
    CollectionRun,
    EventVersion,
    PublicationManifest,
    SourceAssertion,
    UniverseMembership,
)
from wtf_ingestion.publication.trust import publication_trust

CONTRACT_VERSION = "earnings-research-0.1.0"


class EarningsUnavailable(RuntimeError):
    pass


def fiscal_structure(period: str | None) -> dict[str, Any]:
    match = re.fullmatch(r"Q([1-4]) (and FY|FY)?(20\d{2})", period or "")
    return {
        "quarter": int(match[1]) if match else None,
        "year": int(match[3]) if match else None,
        "annual_results_included": bool(match and match[2] == "and FY"),
        "year_basis": "fiscal" if match and match[2] else "source_stated" if match else "unknown",
        "state": "available" if match else "unavailable",
        "reason": None if match else "Fiscal period is not source-supported in this event.",
    }


def read_snapshot(session: Session, cfg: Settings) -> tuple[PublicationManifest, list[FeedRecord]]:
    publication = session.scalar(
        select(PublicationManifest)
        .where(PublicationManifest.activated_at.is_not(None))
        .order_by(PublicationManifest.generated_at.desc())
        .limit(1)
    )
    if publication is None:
        raise EarningsUnavailable("No accepted publication is available.")
    if cfg.dashboard_mode == "live" and publication.evidence_mode != "live":
        raise EarningsUnavailable("Live earnings cannot consume fixture evidence.")
    root = cfg.output_dir.resolve()
    name = publication.output_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    directory = (root / name).resolve()
    if directory.parent != root:
        raise EarningsUnavailable("Publication directory failed containment validation.")
    try:
        pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
        if (
            pointer["publication_id"] != str(publication.id)
            or pointer["manifest"] != f"{name}/manifest.json"
        ):
            raise EarningsUnavailable("Publication pointer and accepted database disagree.")
        manifest_bytes = (directory / "manifest.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != publication.manifest_sha256:
            raise EarningsUnavailable("Publication manifest checksum failed.")
        manifest = json.loads(manifest_bytes)
        if (
            manifest["publication_id"] != str(publication.id)
            or manifest["schema_version"] != "0.2.0"
            or manifest["record_count"] != publication.record_count
            or manifest["evidence_mode"] != publication.evidence_mode
        ):
            raise EarningsUnavailable("Publication identity/schema/count/mode failed validation.")
        for filename, expected in (
            ("events.jsonl", publication.jsonl_sha256),
            ("events.csv", publication.csv_sha256),
        ):
            path = directory / filename
            if path.stat().st_size > 50_000_000:
                raise EarningsUnavailable("Publication file exceeds the consumer byte bound.")
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != expected:
                raise EarningsUnavailable("Publication event-file checksum failed.")
            if filename == "events.jsonl":
                records = [FeedRecord.model_validate_json(line) for line in body.splitlines()]
        if len(records) != publication.record_count:
            raise EarningsUnavailable("Publication record count failed validation.")
        if len({row.event_id for row in records}) != len(records):
            raise EarningsUnavailable("Duplicate event identity in accepted publication.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EarningsUnavailable("Accepted publication is unavailable or malformed.") from exc
    return publication, records


def earnings_data(session: Session, cfg: Settings, *, retained: bool = False) -> dict[str, Any]:
    publication, records = read_snapshot(session, cfg)
    state = publication_trust(session, publication)
    versions = {
        (row.event_id, row.revision_number): row
        for row in session.scalars(
            select(EventVersion).where(EventVersion.event_id.in_([row.event_id for row in records]))
        )
    }
    latest_revisions: dict[UUID, int] = {}
    for known_version in versions.values():
        latest_revisions[known_version.event_id] = max(
            latest_revisions.get(known_version.event_id, 0), known_version.revision_number
        )
    if state == "HEALTHY" and any(
        latest_revisions.get(record.event_id, 0) > record.revision_number for record in records
    ):
        state = "DEGRADED"
    if state != "HEALTHY" and not retained:
        raise EarningsUnavailable(
            f"Calendar trust is {state}; request retained audit data explicitly."
        )
    assertions: dict[UUID, SourceAssertion] = {}
    ranked_assertions = (
        select(
            SourceAssertion.id,
            func.row_number()
            .over(
                partition_by=EventVersion.id,
                order_by=(CollectionRun.finished_at.desc(), SourceAssertion.id.desc()),
            )
            .label("position"),
        )
        .join(EventVersion, SourceAssertion.event_id == EventVersion.event_id)
        .join(CollectionRun, SourceAssertion.run_id == CollectionRun.id)
        .where(
            tuple_(EventVersion.event_id, EventVersion.revision_number).in_(
                [(row.event_id, row.revision_number) for row in records]
            ),
            CollectionRun.finished_at <= publication.generated_at,
            SourceAssertion.observed_at <= publication.generated_at,
            *(
                SourceAssertion.facts[field]
                .as_string()
                .is_not_distinct_from(EventVersion.payload[field].as_string())
                for field in EventCandidate.material_fields
            ),
        )
        .subquery()
    )
    for observed_assertion in session.scalars(
        select(SourceAssertion)
        .join(ranked_assertions, SourceAssertion.id == ranked_assertions.c.id)
        .where(ranked_assertions.c.position == 1)
    ):
        assertions[observed_assertion.event_id] = observed_assertion
    memberships = session.scalars(
        select(UniverseMembership)
        .options(selectinload(UniverseMembership.company))
        .where(UniverseMembership.universe_version_id == publication.universe_version_id)
    ).all()
    by_cik = {row.company.cik: row for row in memberships}
    data: list[dict[str, Any]] = []
    for record in records:
        if record.event_type != "earnings_release":
            continue
        version = versions.get((record.event_id, record.revision_number))
        assertion = assertions.get(record.event_id)
        if version is None or assertion is None or version.content_hash != record.content_hash:
            raise EarningsUnavailable("Exact published event revision/evidence binding is missing.")
        try:
            assertion_hash = EventCandidate.model_validate(assertion.facts).content_hash
        except ValueError as exc:
            raise EarningsUnavailable("Published source assertion is malformed.") from exc
        if assertion_hash != record.content_hash:
            raise EarningsUnavailable("Published event and source assertion disagree.")
        membership = by_cik.get(record.cik)
        estimated = record.date_basis == "estimated"
        evidence = {
            "evidence_id": str(assertion.id),
            "source_url": record.source_url,
            "locator": assertion.evidence_locator,
            "observed_at": as_utc(assertion.observed_at).isoformat(),
            "raw_sha256": assertion.raw_payload_hash,
            "scope": "filing-date projection" if estimated else "explicit forward announcement",
        }
        metadata = assertion.facts.get("metadata", {})
        fiscal = fiscal_structure(record.fiscal_period)
        fields = {
            "event_date": {
                "value": record.event_date.isoformat() if record.event_date else None,
                "state": "estimated" if estimated else "available",
                "reason": "Prior-year filing day plus 52 weeks; not issuer-confirmed."
                if estimated
                else None,
            },
            "session": {
                "value": record.session if record.session != "UNKNOWN" else None,
                "state": "available" if record.session != "UNKNOWN" else "unavailable",
                "reason": "Release session cannot be inferred from a filing-date estimate."
                if estimated
                else "Release session is not supported by this announcement."
                if record.session == "UNKNOWN"
                else None,
            },
            "fiscal_period": {
                "value": record.fiscal_period,
                "state": "available" if record.fiscal_period else "unavailable",
                "reason": fiscal["reason"],
            },
            "release_time": {
                "value": record.scheduled_start.isoformat() if record.scheduled_start else None,
                "state": "available" if record.scheduled_start else "unavailable",
                "reason": None
                if record.scheduled_start
                else "No source-supported exact release clock; a call clock is a separate fact.",
                "timezone": record.source_timezone,
                "precision": record.time_precision,
            },
        }
        data.append(
            {
                "event": record.model_dump(mode="json"),
                "company_id": str(membership.company_id) if membership else None,
                "listings": membership.listings if membership else [],
                "publication_id": str(publication.id),
                "event_revision": record.revision_number,
                "confidence_basis": "low_confidence_filing_projection"
                if estimated
                else "official_explicit_source",
                "fields": fields,
                "fiscal": fiscal,
                "conference_call": metadata.get(
                    "conference_call",
                    {
                        "state": "unavailable",
                        "at": None,
                        "timezone": None,
                        "reason": "No supported call clock in the accepted evidence.",
                    },
                ),
                "evidence": [evidence],
                "estimate": metadata if estimated else None,
                "brief": {
                    "state": "not_generated",
                    "reason": "Grounded research generation has not yet been configured.",
                    "sections": {
                        "what_it_is": {
                            "text": (
                                f"{record.company_name}: {record.event_date}; "
                                + (
                                    "estimated filing-cadence date."
                                    if estimated
                                    else "announced release."
                                )
                            ),
                            "state": "sourced_calendar_summary",
                            "evidence_ids": [str(assertion.id)],
                        },
                        "why_it_matters": {
                            "state": "unavailable",
                            "reason": "No approved issuer-specific research evidence pack yet.",
                        },
                        "historical_reaction": {
                            "state": "unavailable",
                            "reason": "Historical releases and permitted prices are required.",
                        },
                        "what_to_watch": {
                            "state": "unavailable",
                            "reason": "Requires cited issuer-specific research evidence.",
                        },
                    },
                },
                "reaction": {
                    "state": "unavailable",
                    "sample_count": 0,
                    "reason": "No qualified free company-use historical price source is active.",
                },
            }
        )
    represented = {row["event"]["cik"] for row in data}
    missing = [
        {
            "company_id": str(row.company_id),
            "cik": row.company.cik,
            "company_name": row.company.company_name,
            "listings": row.listings,
            "state": "no_supported_date",
            "reason": "No accepted announced date or unambiguous approved forecast basis.",
        }
        for row in memberships
        if row.company.cik not in represented
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "as_of": datetime.now(UTC).isoformat(),
        "publication_id": str(publication.id),
        "publication_generated_at": as_utc(publication.generated_at).isoformat(),
        "calendar_state": state,
        "is_current": state == "HEALTHY",
        "brief_state": "not_generated",
        "price_state": "unavailable",
        "data": data,
        "missing_issuers": missing,
        "counts": {
            "records": len(data),
            "announced": sum(row["event"]["date_basis"] == "announced" for row in data),
            "estimated": sum(row["event"]["date_basis"] == "estimated" for row in data),
            "missing_issuers": len(missing),
        },
    }
