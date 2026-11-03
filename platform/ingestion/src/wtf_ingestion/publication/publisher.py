from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.domain.enums import RunState
from wtf_ingestion.domain.models import EventCandidate, FeedRecord
from wtf_ingestion.health import aggregate_feed_state, as_utc, evaluate_run_state
from wtf_ingestion.persistence.locking import pipeline_lock
from wtf_ingestion.persistence.models import (
    CollectionRun,
    EventCurrent,
    EventVersion,
    PublicationManifest,
    SourceAssertion,
)
from wtf_ingestion.publication.files import replace_atomic
from wtf_ingestion.publication.trust import write_status
from wtf_ingestion.selection import without_shadowed_projections
from wtf_ingestion.universe import active_universe, in_fixed_universe


@dataclass(frozen=True)
class PublicationResult:
    publication_id: UUID
    directory: Path
    record_count: int
    state: str
    jsonl_sha256: str
    csv_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Publisher:
    schema_version = "0.2.0"

    def __init__(self, session: Session, output_dir: Path, horizon_days: int = 90):
        self.session = session
        self.output_dir = output_dir
        self.horizon_days = horizon_days

    def publish(self, evidence_mode: str = "fixture") -> PublicationResult:
        with pipeline_lock(self.session):
            try:
                return self._publish(evidence_mode)
            except Exception:
                self.session.rollback()
                write_status(self.session, self.output_dir)
                raise

    def recover_pointer(self, publication_id: UUID) -> None:
        """Explicit recovery of a validated manifest committed before a pointer failure."""
        with pipeline_lock(self.session):
            publication = self.session.get(PublicationManifest, publication_id)
            if publication is None or publication.activated_at is not None:
                raise ValueError("select an unactivated committed publication")
            directory = (self.output_dir / publication.output_path).resolve()
            if directory.parent != self.output_dir.resolve():
                raise ValueError("publication directory is outside the configured output root")
            if _sha256(directory / "manifest.json") != publication.manifest_sha256:
                raise RuntimeError("publication manifest checksum mismatch")
            self._validate(
                directory,
                publication.record_count,
                publication.jsonl_sha256,
                publication.csv_sha256,
            )
            state = aggregate_feed_state(
                [
                    evaluate_run_state(run)
                    for run in self._latest_runs(publication.evidence_mode).values()
                ]
            )
            if (
                state != RunState.HEALTHY
                or publication.freshness_deadline is None
                or as_utc(publication.freshness_deadline) <= datetime.now(UTC)
            ):
                raise RuntimeError("publication recovery refused: evidence is not current")
            self._write_text_atomic(
                self.output_dir / "current.json",
                json.dumps(
                    {
                        "publication_id": str(publication.id),
                        "manifest": f"{directory.name}/manifest.json",
                        "generated_at": publication.generated_at.isoformat(),
                        "freshness_deadline": as_utc(publication.freshness_deadline).isoformat(),
                        "evidence_mode": publication.evidence_mode,
                        "trust_check_required": True,
                        "status_file": "status.json",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            publication.activated_at = datetime.now(UTC)
            self.session.commit()
            write_status(self.session, self.output_dir)

    def _publish(self, evidence_mode: str) -> PublicationResult:
        if evidence_mode not in {"fixture", "live"}:
            raise ValueError("evidence mode must be fixture or live")
        generated_at = datetime.now(UTC)
        horizon_start = generated_at.date()
        horizon_end = horizon_start + timedelta(days=self.horizon_days)
        rows = self.session.scalars(
            select(EventCurrent)
            .where(EventCurrent.event_date >= horizon_start, EventCurrent.event_date <= horizon_end)
            .order_by(EventCurrent.event_date, EventCurrent.ticker, EventCurrent.title)
        ).all()
        universe = active_universe(self.session)
        rows = without_shadowed_projections(in_fixed_universe(self.session, rows))
        if not rows:
            raise RuntimeError("publication refused: no accepted current records in the horizon")
        latest_runs = self._latest_runs(evidence_mode)
        state = aggregate_feed_state(
            [evaluate_run_state(run) for run in latest_runs.values()]
        ).value
        if state != RunState.HEALTHY.value:
            raise RuntimeError(
                f"publication refused: latest source state is {state}; "
                "last-known-good pointer is preserved"
            )
        evidence_runs: dict[UUID, CollectionRun] = {}
        freshness_runs: dict[UUID, CollectionRun] = dict(latest_runs)
        for row in rows:
            assertion = self.session.scalar(
                select(SourceAssertion)
                .join(CollectionRun, CollectionRun.id == SourceAssertion.run_id)
                .where(
                    SourceAssertion.event_id == row.event_id,
                    SourceAssertion.source_id == row.source_id,
                    SourceAssertion.source_event_id == row.source_event_id,
                )
                .order_by(CollectionRun.started_at.desc(), SourceAssertion.observed_at.desc())
                .limit(1)
            )
            version = self.session.get(EventVersion, row.current_version_id)
            if assertion is None or version is None:
                raise RuntimeError("publication refused: event evidence is incomplete")
            material = EventCandidate.model_validate(version.payload).material_dict()
            if any(assertion.facts.get(key) != value for key, value in material.items()):
                raise RuntimeError(
                    "publication refused: latest observation conflicts with current facts"
                )
            for run_id in (assertion.run_id, version.run_id):
                run = self.session.get(CollectionRun, run_id)
                if run is None or run.evidence_mode != evidence_mode:
                    raise RuntimeError(
                        "publication refused: fixture/live evidence cannot be mixed or relabelled"
                    )
                evidence_runs[run_id] = run
            current_run = latest_runs.get(row.source_id)
            if current_run is None or evaluate_run_state(current_run) not in {
                RunState.HEALTHY,
                RunState.VALID_EMPTY,
            }:
                raise RuntimeError("publication refused: source freshness is not verified")
            # A fresh successful run does not refresh an event that it did not observe.
            observation_run = evidence_runs[assertion.run_id]
            freshness_runs[observation_run.id] = observation_run
            if evaluate_run_state(observation_run) not in {RunState.HEALTHY, RunState.VALID_EMPTY}:
                raise RuntimeError("publication refused: event observation is stale")
        included_run_ids = sorted(str(run_id) for run_id in evidence_runs)
        if (
            evidence_mode == "live"
            and universe
            and any(
                run.source.key == "sec_edgar" and run.universe_version_id != universe.id
                for run in latest_runs.values()
            )
        ):
            raise RuntimeError("publication refused: selected universe has no complete SEC scan")
        deadlines = [
            as_utc(run.freshness_deadline)
            for run in freshness_runs.values()
            if run.freshness_deadline
        ]
        freshness_deadline = min(deadlines) if deadlines else generated_at
        records = [self._to_feed_record(row) for row in rows]
        publication_id = uuid4()
        directory = self.output_dir / generated_at.strftime("%Y%m%dT%H%M%S.%fZ")
        directory.mkdir(parents=True, exist_ok=False)
        jsonl_path = directory / "events.jsonl"
        csv_path = directory / "events.csv"
        manifest_path = directory / "manifest.json"
        self._write_jsonl_atomic(jsonl_path, records)
        self._write_csv_atomic(csv_path, records)
        jsonl_hash = _sha256(jsonl_path)
        csv_hash = _sha256(csv_path)
        manifest_payload = {
            "universe_version": universe.version if universe else None,
            "publication_id": str(publication_id),
            "schema_version": self.schema_version,
            "generated_at": generated_at.isoformat(),
            "freshness_deadline": freshness_deadline.isoformat(),
            "coverage_window": {"start": horizon_start.isoformat(), "end": horizon_end.isoformat()},
            "state": state,
            "record_count": len(records),
            "evidence_mode": evidence_mode,
            "date_basis_counts": {
                kind: sum(record.date_basis == kind for record in records)
                for kind in ("announced", "estimated", "official_schedule")
            },
            "history_policy": {"retain_at_least_years": 2, "initial_backfill": False},
            "included_run_ids": included_run_ids,
            "source_states": {
                str(key): evaluate_run_state(run).value for key, run in latest_runs.items()
            },
            "files": {
                "events.jsonl": {"sha256": jsonl_hash, "rows": len(records)},
                "events.csv": {"sha256": csv_hash, "rows": len(records)},
            },
        }
        self._write_text_atomic(
            manifest_path, json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n"
        )
        manifest_hash = _sha256(manifest_path)
        self._validate(directory, len(records), jsonl_hash, csv_hash)
        publication = PublicationManifest(
            universe_version_id=universe.id if universe else None,
            id=publication_id,
            schema_version=self.schema_version,
            generated_at=generated_at,
            freshness_deadline=freshness_deadline,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            state=state,
            record_count=len(records),
            jsonl_sha256=jsonl_hash,
            csv_sha256=csv_hash,
            manifest_sha256=manifest_hash,
            output_path=directory.name,
            included_run_ids=included_run_ids,
            evidence_mode=evidence_mode,
        )
        self.session.add(publication)
        self.session.commit()
        # The pointer can only reference validated files and a committed manifest.
        # A crash here leaves a recoverable, unactivated manifest, not an invalid current feed.
        self._write_text_atomic(
            self.output_dir / "current.json",
            json.dumps(
                {
                    "publication_id": str(publication_id),
                    "manifest": f"{directory.name}/manifest.json",
                    "generated_at": generated_at.isoformat(),
                    "freshness_deadline": freshness_deadline.isoformat(),
                    "evidence_mode": evidence_mode,
                    "trust_check_required": True,
                    "status_file": "status.json",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        publication.activated_at = datetime.now(UTC)
        self.session.commit()
        write_status(self.session, self.output_dir)
        return PublicationResult(
            publication_id=publication_id,
            directory=directory,
            record_count=len(records),
            state=state,
            jsonl_sha256=jsonl_hash,
            csv_sha256=csv_hash,
        )

    def _latest_runs(self, evidence_mode: str) -> dict[UUID, CollectionRun]:
        runs = self.session.scalars(
            select(CollectionRun)
            .where(CollectionRun.evidence_mode == evidence_mode)
            .order_by(CollectionRun.started_at.desc())
        ).all()
        latest: dict[UUID, CollectionRun] = {}
        for run in runs:
            latest.setdefault(run.source_id, run)
        return latest

    def _to_feed_record(self, row: EventCurrent) -> FeedRecord:
        version = self.session.get(EventVersion, row.current_version_id)
        if version is None:
            raise RuntimeError("current revision evidence is missing")
        metadata = version.payload.get("metadata", {})
        return FeedRecord(
            event_id=row.event_id,
            revision_number=row.revision_number,
            event_type=row.event_type,
            ticker=row.ticker,
            exchange=row.exchange,
            company_name=row.company_name,
            cik=row.cik,
            title=row.title,
            event_date=row.event_date,
            scheduled_start=row.scheduled_start,
            source_timezone=row.source_timezone,
            session=row.session,
            fiscal_period=row.fiscal_period,
            status=row.status,
            time_precision=row.time_precision,
            source_id=row.source_key,
            source_event_id=row.source_event_id,
            source_url=row.source_url,
            collected_at=row.collected_at,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
            content_hash=row.content_hash,
            parser_version=row.parser_version,
            date_basis=(
                "estimated"
                if row.status == "estimated"
                else "official_schedule"
                if row.event_type == "economic_release"
                else "announced"
            ),
            estimate_method=metadata.get("estimate_method"),
            estimate_basis_date=metadata.get("estimate_basis_date"),
            estimate_basis_url=metadata.get("estimate_basis_url"),
            estimate_confidence=metadata.get("estimate_confidence"),
        )

    def _write_jsonl_atomic(self, path: Path, records: list[FeedRecord]) -> None:
        content = "".join(
            json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n" for record in records
        )
        self._write_text_atomic(path, content)

    def _write_csv_atomic(self, path: Path, records: list[FeedRecord]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        fieldnames = list(records[0].model_dump(mode="json").keys())
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for record in records:
                writer.writerow(record.model_dump(mode="json"))
            handle.flush()
            os.fsync(handle.fileno())
        replace_atomic(temporary, path)

    def _write_text_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_atomic(temporary, path)

    def _validate(self, directory: Path, expected: int, jsonl_hash: str, csv_hash: str) -> None:
        jsonl_path = directory / "events.jsonl"
        csv_path = directory / "events.csv"
        with jsonl_path.open(encoding="utf-8") as handle:
            jsonl_rows = [json.loads(line) for line in handle if line.strip()]
        with csv_path.open(encoding="utf-8", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
        if len(jsonl_rows) != expected or len(csv_rows) != expected:
            raise RuntimeError("publication row-count validation failed")
        if _sha256(jsonl_path) != jsonl_hash or _sha256(csv_path) != csv_hash:
            raise RuntimeError("publication checksum validation failed")
