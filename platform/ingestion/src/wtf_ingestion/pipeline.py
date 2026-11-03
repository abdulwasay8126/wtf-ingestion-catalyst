from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.domain.enums import CoverageState, EventStatus, RunState, TimePrecision
from wtf_ingestion.domain.models import EventCandidate
from wtf_ingestion.health import as_utc
from wtf_ingestion.logging import get_logger
from wtf_ingestion.persistence.locking import pipeline_lock
from wtf_ingestion.persistence.models import (
    CollectionRun,
    Company,
    EventCurrent,
    EventVersion,
    QuarantineRecord,
    RawObject,
    Source,
    SourceAssertion,
    UniverseMembership,
)
from wtf_ingestion.publication.trust import write_status
from wtf_ingestion.safety import validate_public_url


@dataclass(frozen=True)
class IngestResult:
    run_id: UUID
    state: str
    received: int
    inserted: int
    revised: int
    unchanged: int


class ReconciliationConflict(ValueError):
    """Unorderable conflicting evidence must not change the current snapshot."""

    def __init__(self, candidate: EventCandidate):
        super().__init__("conflicting announcement requires review")
        self.candidate = candidate


class IngestionService:
    def __init__(
        self,
        session: Session,
        raw_dir: Path,
        freshness_hours: int = 36,
        output_dir: Path | None = None,
        allow_estimates: bool = False,
    ):
        self.session = session
        self.raw_dir = raw_dir
        self.freshness_hours = freshness_hours
        self.output_dir = output_dir
        self.allow_estimates = allow_estimates
        self.log = get_logger()

    def ingest(self, **kwargs) -> IngestResult:  # type: ignore[no-untyped-def]
        with pipeline_lock(self.session):
            try:
                return self._ingest(**kwargs)
            finally:
                if self.output_dir:
                    write_status(self.session, self.output_dir)

    def collect(
        self,
        *,
        source_key: str,
        fetch: Callable[[UUID], tuple[bytes, str, str]],
        parse: Callable[[bytes, datetime], list[EventCandidate]],
        trigger: str = "manual",
        evidence_mode: str = "live",
        allow_empty: bool = False,
    ) -> IngestResult:
        """Ledger fetch/parse failures too; capture complete raw input before parsing."""
        with pipeline_lock(self.session):
            source = self.session.scalar(select(Source).where(Source.key == source_key))
            if source is None:
                raise ValueError("source is not registered")
            self._check_mode(source, evidence_mode)
            now = datetime.now(UTC)
            run = CollectionRun(
                source_id=source.id,
                trigger=trigger,
                evidence_mode=evidence_mode,
                state=RunState.RUNNING.value,
                stage="fetch",
                started_at=now,
                freshness_deadline=now + timedelta(hours=source.freshness_hours),
            )
            self.session.add(run)
            self.session.commit()
            try:
                raw, url, content_type = fetch(run.id)
                validate_public_url(url)
                raw_hash = hashlib.sha256(raw).hexdigest()
                path = self._store_raw(raw_hash, raw)
                self.session.add(
                    RawObject(
                        run_id=run.id,
                        sha256=raw_hash,
                        byte_size=len(raw),
                        content_type=content_type,
                        source_url=url,
                        storage_path=str(path.resolve()),
                        retrieved_at=datetime.now(UTC),
                        parser_version=source.parser_version,
                    )
                )
                run.stage = "parse"
                self.session.commit()
                candidates = parse(raw, datetime.now(UTC))
                return self._ingest(
                    source_key=source_key,
                    candidates=candidates,
                    raw=raw,
                    source_url=url,
                    content_type=content_type,
                    trigger=trigger,
                    evidence_mode=evidence_mode,
                    allow_empty=allow_empty,
                    existing_run=run,
                )
            except Exception as exc:
                self._fail_run(run.id, exc)
                raise
            finally:
                if self.output_dir:
                    write_status(self.session, self.output_dir)

    @staticmethod
    def _check_mode(source: Source, evidence_mode: str) -> None:
        if evidence_mode not in {"fixture", "live"}:
            raise ValueError("evidence mode must be fixture or live")
        if evidence_mode == "live" and (
            source.fixture_only or not source.live_enabled or source.permission_status != "approved"
        ):
            raise RuntimeError("live source access has not been approved and enabled")

    def _fail_run(self, run_id: UUID, exc: Exception) -> None:
        self.session.rollback()
        failed = self.session.get(CollectionRun, run_id)
        if failed and failed.state != RunState.FAILED.value:
            failed.state = RunState.FAILED.value
            failed.finished_at = datetime.now(UTC)
            failed.safe_error_code = getattr(exc, "code", "INGESTION_FAILED")
            failed.safe_error_summary = exc.__class__.__name__
            if failed.safe_error_code == "PAYLOAD_ERROR":
                failed.safe_error_summary = (
                    "Input failed structural checks; "
                    "inspect retained raw evidence and parser version."
                )
            elif failed.safe_error_code == "SEC_SCAN_INCOMPLETE":
                failed.safe_error_summary = (
                    "Company checks finished with unresolved evidence. Inspect the partial scan "
                    "preview and company reviews; no new official feed was published."
                )
            elif failed.safe_error_code == "TRANSPORT_ERROR":
                failed.safe_error_summary = (
                    "Source request failed; review HTTP attempts and access policy before retrying."
                )
            elif failed.safe_error_code in {"SOURCE_ACCESS_BLOCKED", "SOURCE_THROTTLED"}:
                failed.safe_error_summary = (
                    "Access notice: collection stopped. Notify the operator immediately "
                    "and review with the source; do not bypass the block."
                )
                source = self.session.get(Source, failed.source_id)
                if source:
                    source.live_enabled = False
                    source.permission_status = "access_review_required"
            self.session.commit()

    def _ingest(
        self,
        *,
        source_key: str,
        candidates: list[EventCandidate],
        raw: bytes,
        source_url: str,
        content_type: str,
        trigger: str = "manual",
        evidence_mode: str = "fixture",
        allow_empty: bool = False,
        existing_run: CollectionRun | None = None,
    ) -> IngestResult:
        source = self.session.scalar(select(Source).where(Source.key == source_key))
        if source is None:
            raise ValueError(f"source is not registered: {source_key}")
        self._check_mode(source, evidence_mode)
        validate_public_url(source_url)
        now = datetime.now(UTC)
        run = existing_run or CollectionRun(
            source_id=source.id,
            trigger=trigger,
            evidence_mode=evidence_mode,
            state=RunState.RUNNING.value,
            stage="raw_capture",
            started_at=now,
            freshness_deadline=now + timedelta(hours=source.freshness_hours),
            received_count=len(candidates),
        )
        self.session.add(run)
        self.session.commit()
        run_id = run.id
        raw_hash = hashlib.sha256(raw).hexdigest()
        try:
            raw_path = self._store_raw(raw_hash, raw)
            if existing_run is None:
                self.session.add(
                    RawObject(
                        run_id=run.id,
                        sha256=raw_hash,
                        byte_size=len(raw),
                        content_type=content_type,
                        source_url=source_url,
                        storage_path=str(raw_path.resolve()),
                        retrieved_at=now,
                        parser_version=source.parser_version,
                    )
                )
            run.stage = "validation"
            run.received_count = len(candidates)
            run.response_content_type = content_type
            run.response_bytes = len(raw)
            self.session.commit()
            if not candidates:
                run.state = (
                    RunState.VALID_EMPTY.value if allow_empty else RunState.SUSPICIOUS_EMPTY.value
                )
                run.stage = "validation"
                run.finished_at = datetime.now(UTC)
                if not allow_empty:
                    run.safe_error_code = "UNEXPECTED_ZERO_RECORDS"
                    run.safe_error_summary = (
                        "Zero records violated this source's fixture expectation."
                    )
                    run.anomaly_flags = ["unexpected_zero_records"]
                self.session.commit()
                return IngestResult(run.id, run.state, 0, 0, 0, 0)
            unique: dict[str, EventCandidate] = {}
            conflicts: list[EventCandidate] = []
            for candidate in candidates:
                if candidate.source_id != source_key:
                    raise ValueError("candidate source does not match the selected source")
                if candidate.status == EventStatus.ESTIMATED:
                    self._check_estimate(candidate)
                previous = unique.get(candidate.canonical_key)
                if previous and previous.content_hash != candidate.content_hash:
                    conflicts.append(candidate)
                elif previous:
                    run.duplicate_count += 1
                else:
                    unique[candidate.canonical_key] = candidate
            if conflicts:
                for candidate in conflicts:
                    self.session.add(
                        QuarantineRecord(
                            run_id=run.id,
                            source_event_id=candidate.source_event_id,
                            reason_code="CONFLICTING_DUPLICATE",
                            safe_summary="Conflicting facts for one identity; batch withheld.",
                            payload_hash=candidate.content_hash,
                        )
                    )
                run.state = RunState.DEGRADED.value
                run.rejected_count = len(candidates)
                run.safe_error_code = "CONFLICTING_DUPLICATE"
                run.safe_error_summary = (
                    "Conflicting duplicate facts were quarantined; current events are unchanged."
                )
                run.finished_at = datetime.now(UTC)
                self.session.commit()
                return IngestResult(run.id, run.state, len(candidates), 0, 0, 0)
            run.stage = "normalize"
            self.session.commit()
            inserted = revised = unchanged = 0
            for candidate in unique.values():
                candidate.raw_payload_hash = raw_hash
                current = self._resolve_current(source, candidate)
                if current is None:
                    current = self._insert_current(source, run, candidate, raw_hash)
                    inserted += 1
                elif current.content_hash != candidate.content_hash and self._may_supersede(
                    source, current, candidate
                ):
                    self._revise_current(current, run, candidate, raw_hash)
                    current.source_id = source.id
                    current.source_key = source.key
                    current.source_event_id = candidate.source_event_id
                    revised += 1
                else:
                    if (
                        current.source_id == source.id
                        and current.source_event_id == candidate.source_event_id
                        and current.content_hash == candidate.content_hash
                    ):
                        current.last_seen_at = candidate.collected_at
                        current.collected_at = candidate.collected_at
                    unchanged += 1
                self.session.add(
                    SourceAssertion(
                        run_id=run.id,
                        source_id=source.id,
                        event_id=current.event_id,
                        source_event_id=candidate.source_event_id,
                        evidence_locator=candidate.evidence_locator,
                        evidence_strength=(
                            "derived_estimate"
                            if candidate.status == EventStatus.ESTIMATED
                            else "fixture_explicit"
                            if evidence_mode == "fixture"
                            else "official_explicit"
                        ),
                        facts=candidate.model_dump(mode="json"),
                        observed_at=candidate.collected_at,
                        raw_payload_hash=raw_hash,
                    )
                )
                self._update_coverage(candidate)
            run.state = RunState.HEALTHY.value
            run.stage = "persisted"
            run.finished_at = datetime.now(UTC)
            run.accepted_count = len(unique)
            run.inserted_count = inserted
            run.revised_count = revised
            run.unchanged_count = unchanged
            self.session.commit()
            self.log.info(
                "ingestion_complete",
                source=source_key,
                run_id=str(run.id),
                state=run.state,
                inserted=inserted,
                revised=revised,
                unchanged=unchanged,
            )
            return IngestResult(run.id, run.state, len(candidates), inserted, revised, unchanged)
        except ReconciliationConflict as exc:
            # Raw capture and the run ledger were already committed. Roll back the
            # entire candidate batch, including any earlier revisions/assertions.
            self.session.rollback()
            quarantined_run = self.session.get(CollectionRun, run_id)
            if quarantined_run is None:
                raise RuntimeError("quarantine run ledger is missing") from exc
            self.session.add(
                QuarantineRecord(
                    run_id=run_id,
                    source_event_id=exc.candidate.source_event_id,
                    reason_code="AMBIGUOUS_SOURCE_CONFLICT",
                    safe_summary="Conflicting or ambiguous announcement linkage; batch withheld.",
                    payload_hash=exc.candidate.content_hash,
                )
            )
            quarantined_run.state = RunState.DEGRADED.value
            quarantined_run.rejected_count = len(candidates)
            quarantined_run.safe_error_code = "AMBIGUOUS_SOURCE_CONFLICT"
            quarantined_run.safe_error_summary = (
                "Conflicting evidence cannot be ordered safely; inspect retained raw evidence."
            )
            quarantined_run.finished_at = datetime.now(UTC)
            self.session.commit()
            return IngestResult(run_id, quarantined_run.state, len(candidates), 0, 0, 0)
        except Exception as exc:
            self._fail_run(run_id, exc)
            self.log.error(
                "ingestion_failed",
                source=source_key,
                run_id=str(run_id),
                error_code=getattr(exc, "code", "INGESTION_FAILED"),
            )
            raise

    def _resolve_current(self, source: Source, candidate: EventCandidate) -> EventCurrent | None:
        assertion = self.session.scalar(
            select(SourceAssertion)
            .where(
                SourceAssertion.source_id == source.id,
                SourceAssertion.source_event_id == candidate.source_event_id,
            )
            .order_by(SourceAssertion.observed_at.desc())
            .limit(1)
        )
        if assertion:
            return self.session.get(EventCurrent, assertion.event_id)
        current = self.session.scalar(
            select(EventCurrent).where(EventCurrent.canonical_key == candidate.canonical_key)
        )
        if current:
            return current
        # Never guess a quarter or match on similar titles. Exact period + CIK only.
        if (
            candidate.cik
            and candidate.fiscal_period
            and candidate.event_type.value == "earnings_release"
        ):
            matches = self.session.scalars(
                select(EventCurrent).where(
                    EventCurrent.cik == candidate.cik,
                    EventCurrent.fiscal_period == candidate.fiscal_period,
                    EventCurrent.event_type == candidate.event_type.value,
                )
            ).all()
            if len(matches) > 1:
                raise ReconciliationConflict(candidate)
            if matches:
                return matches[0]
        return None

    def _may_supersede(
        self, source: Source, current: EventCurrent, candidate: EventCandidate
    ) -> bool:
        if candidate.status == EventStatus.ESTIMATED and current.status != "estimated":
            return False
        if candidate.status != EventStatus.ESTIMATED and current.status == "estimated":
            return True
        previous = self.session.get(Source, current.source_id)
        rank = {"issuer_ir": 3, "regulatory_filing": 2, "official_ics": 2}
        if previous is None:
            return True
        incoming_rank = rank.get(source.source_type, 0)
        previous_rank = rank.get(previous.source_type, 0)
        if incoming_rank != previous_rank:
            return incoming_rank > previous_rank
        version = self.session.get(EventVersion, current.current_version_id)
        if version is None:
            raise RuntimeError("current revision evidence is missing")
        previous_payload = version.payload
        incoming = candidate.model_dump(mode="json")
        incoming_clock = self._publication_bounds(incoming)
        previous_clock = self._publication_bounds(previous_payload)
        latest_assertion = self.session.scalar(
            select(SourceAssertion)
            .join(CollectionRun, CollectionRun.id == SourceAssertion.run_id)
            .where(
                SourceAssertion.event_id == current.event_id,
                SourceAssertion.source_id == current.source_id,
                SourceAssertion.source_event_id == current.source_event_id,
            )
            .order_by(CollectionRun.started_at.desc(), SourceAssertion.observed_at.desc())
            .limit(1)
        )
        if latest_assertion:
            material = EventCandidate.model_validate(previous_payload).material_dict()
            if all(latest_assertion.facts.get(key) == value for key, value in material.items()):
                previous_clock = self._publication_bounds(latest_assertion.facts) or previous_clock
        same_identity = (
            source.id == current.source_id and candidate.source_event_id == current.source_event_id
        )
        if incoming_clock and previous_clock:
            if incoming_clock[1] < previous_clock[0]:
                if same_identity:
                    raise ReconciliationConflict(candidate)
                return False
            if incoming_clock[0] > previous_clock[1]:
                return True
        if same_identity:
            # A mutable official record can revise its own facts. Older explicit
            # timestamps above still cannot roll it back.
            return True
        event_fields = (
            "event_type",
            "event_date",
            "scheduled_start",
            "source_timezone",
            "session",
            "fiscal_period",
            "status",
            "time_precision",
        )
        if all(incoming.get(key) == previous_payload.get(key) for key in event_fields):
            # Equivalent same-authority documents corroborate the selected record;
            # switching URLs/identities on every refresh creates spurious revisions.
            # Every assertion is still retained, and the selected identity may
            # advance its own parser/facts through the same_identity path above.
            return False
        raise ReconciliationConflict(candidate)

    def _check_estimate(self, candidate: EventCandidate) -> None:
        if not self.allow_estimates:
            raise ValueError("the estimated layer is disabled")
        metadata = candidate.metadata
        basis = date.fromisoformat(str(metadata.get("estimate_basis_date", "")))
        accession = str(metadata.get("estimate_basis_accession", ""))
        expected_root = (
            f"https://www.sec.gov/Archives/edgar/data/{int(candidate.cik or '0')}/"
            f"{accession.replace('-', '')}/"
        )
        if (
            candidate.source_id != "sec_edgar"
            or candidate.event_type.value != "earnings_release"
            or metadata.get("estimate_method") != "sec_item_2_02_52_week_v1"
            or candidate.parser_version != "sec_item_2_02_52_week_v1"
            or metadata.get("basis_is_filing_date_proxy") is not True
            or metadata.get("estimate_confidence") != "low"
            or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
            or "2.02"
            not in re.findall(r"\d+\.\d{2}", str(metadata.get("estimate_basis_items", "")))
            or not str(candidate.source_url).startswith(expected_root)
            or candidate.source_event_id != f"projection:{accession}"
            or candidate.cik is None
            or basis > candidate.collected_at.date()
            or candidate.event_date is None
            or not candidate.collected_at.date()
            <= candidate.event_date
            <= candidate.collected_at.date() + timedelta(days=90)
            or not re.fullmatch(r"[a-f0-9]{64}", str(metadata.get("estimate_basis_raw_sha256", "")))
            or metadata.get("estimate_basis_url") != str(candidate.source_url)
            or candidate.event_date != basis + timedelta(weeks=52)
            or candidate.scheduled_start is not None
            or candidate.time_precision.value != "date_only"
            or candidate.session.value != "UNKNOWN"
            or candidate.fiscal_period is not None
        ):
            raise ValueError("estimated event lacks the approved deterministic basis")

    @staticmethod
    def _publication_bounds(payload: dict) -> tuple[datetime, datetime] | None:  # type: ignore[type-arg]
        """Compare precision-aware source evidence, never collection/arrival order."""
        timestamp = payload.get("source_updated_at") or payload.get("source_published_at")
        if timestamp:
            value = datetime.fromisoformat(str(timestamp)).astimezone(UTC)
            return value, value
        metadata = payload.get("metadata", {})
        filed_on = metadata.get("filed_on") if isinstance(metadata, dict) else None
        if filed_on:
            day = date.fromisoformat(str(filed_on))
            # A filing day cannot order two announcements within that same day.
            return datetime.combine(day, time.min, UTC), datetime.combine(day, time.max, UTC)
        return None

    def _store_raw(self, raw_hash: str, raw: bytes) -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"{raw_hash}.bin"
        if not path.exists():
            temporary = self.raw_dir / f".{raw_hash}.{uuid4().hex}.tmp"
            temporary.write_bytes(raw)
            temporary.replace(path)
        return path

    def _insert_current(
        self, source: Source, run: CollectionRun, candidate: EventCandidate, raw_hash: str
    ) -> EventCurrent:
        event_id = uuid4()
        now = candidate.collected_at
        version = EventVersion(
            event_id=event_id,
            canonical_key=candidate.canonical_key,
            revision_number=1,
            run_id=run.id,
            content_hash=candidate.content_hash,
            payload=candidate.model_dump(mode="json"),
            valid_from=now,
        )
        self.session.add(version)
        self.session.flush()
        current = EventCurrent(
            event_id=event_id,
            canonical_key=candidate.canonical_key,
            source_id=source.id,
            source_key=source.key,
            source_event_id=candidate.source_event_id,
            event_type=candidate.event_type.value,
            ticker=candidate.ticker,
            exchange=candidate.exchange,
            company_name=candidate.company_name,
            cik=candidate.cik,
            title=candidate.title,
            event_date=candidate.event_date,
            scheduled_start=candidate.scheduled_start,
            source_timezone=candidate.source_timezone,
            session=candidate.session.value,
            fiscal_period=candidate.fiscal_period,
            status=candidate.status.value,
            time_precision=candidate.time_precision.value,
            source_url=str(candidate.source_url),
            collected_at=now,
            first_seen_at=min(as_utc(run.started_at), now),
            last_seen_at=now,
            content_hash=candidate.content_hash,
            raw_payload_hash=raw_hash,
            parser_version=candidate.parser_version,
            revision_number=1,
            current_version_id=version.id,
        )
        self.session.add(current)
        self.session.flush()
        return current

    def _revise_current(
        self,
        current: EventCurrent,
        run: CollectionRun,
        candidate: EventCandidate,
        raw_hash: str,
    ) -> None:
        revision = current.revision_number + 1
        version = EventVersion(
            event_id=current.event_id,
            canonical_key=current.canonical_key,
            revision_number=revision,
            run_id=run.id,
            content_hash=candidate.content_hash,
            payload=candidate.model_dump(mode="json"),
            valid_from=candidate.collected_at,
        )
        self.session.add(version)
        self.session.flush()
        for name, value in (
            ("event_type", candidate.event_type.value),
            ("ticker", candidate.ticker),
            ("exchange", candidate.exchange),
            ("company_name", candidate.company_name),
            ("cik", candidate.cik),
            ("title", candidate.title),
            ("event_date", candidate.event_date),
            ("scheduled_start", candidate.scheduled_start),
            ("source_timezone", candidate.source_timezone),
            ("session", candidate.session.value),
            ("fiscal_period", candidate.fiscal_period),
            ("status", candidate.status.value),
            ("time_precision", candidate.time_precision.value),
            ("source_url", str(candidate.source_url)),
            ("collected_at", candidate.collected_at),
            ("last_seen_at", candidate.collected_at),
            ("content_hash", candidate.content_hash),
            ("raw_payload_hash", raw_hash),
            ("parser_version", candidate.parser_version),
            ("revision_number", revision),
            ("current_version_id", version.id),
        ):
            setattr(current, name, value)

    def _update_coverage(self, candidate: EventCandidate) -> None:
        if not candidate.ticker:
            return
        company = self.session.scalar(select(Company).where(Company.ticker == candidate.ticker))
        if company is None:
            return
        memberships = self.session.scalars(
            select(UniverseMembership).where(UniverseMembership.company_id == company.id)
        ).all()
        if candidate.status == EventStatus.ESTIMATED:
            coverage = CoverageState.ESTIMATED.value
        elif candidate.time_precision == TimePrecision.DATE_ONLY:
            coverage = CoverageState.ANNOUNCED_DATE_ONLY.value
        else:
            coverage = CoverageState.CONFIRMED.value
        for membership in memberships:
            membership.coverage_state = coverage
            membership.state_reason = f"Accepted {candidate.source_id} assertion."
