from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from wtf_ingestion.persistence.database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(64), nullable=False)
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    cik: Mapped[str | None] = mapped_column(String(10), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    aliases: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # CIK is the immutable issuer identity; a listing can pass to a documented successor.


class UniverseVersion(Base, TimestampMixin):
    __tablename__ = "universe_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    assumption: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class UniverseMembership(Base):
    __tablename__ = "universe_memberships"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    universe_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("universe_versions.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    coverage_state: Mapped[str] = mapped_column(
        String(40), default="not_yet_announced", nullable=False
    )
    state_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    company: Mapped[Company] = relationship()
    listings: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (
        UniqueConstraint("universe_version_id", "company_id", name="uq_universe_membership"),
    )


class Source(Base, TimestampMixin):
    __tablename__ = "sources"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    permission_status: Mapped[str] = mapped_column(String(50), nullable=False)
    access_method: Mapped[str] = mapped_column(String(50), nullable=False)
    rate_policy: Mapped[str] = mapped_column(String(200), nullable=False)
    freshness_hours: Mapped[int] = mapped_column(Integer, default=36, nullable=False)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fixture_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reviewed_at: Mapped[date] = mapped_column(Date, nullable=False)
    next_review_at: Mapped[date | None] = mapped_column(Date)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    limitations: Mapped[str] = mapped_column(Text, nullable=False)


class SourceEndpoint(Base, TimestampMixin):
    __tablename__ = "source_endpoints"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    terms_url: Mapped[str | None] = mapped_column(Text)
    robots_url: Mapped[str | None] = mapped_column(Text)
    approval_state: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[Source] = relationship()


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(50), nullable=False)
    evidence_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    freshness_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    safe_error_summary: Mapped[str | None] = mapped_column(Text)
    http_status_class: Mapped[str | None] = mapped_column(String(20))
    response_content_type: Mapped[str | None] = mapped_column(String(200))
    response_bytes: Mapped[int | None] = mapped_column(BigInteger)
    received_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revised_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    anomaly_flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    universe_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("universe_versions.id"))
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[Source] = relationship()


class RawObject(Base):
    __tablename__ = "raw_objects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    retained_until: Mapped[date | None] = mapped_column(Date)


class SourceAssertion(Base):
    __tablename__ = "source_assertions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    source_event_id: Mapped[str] = mapped_column(String(300), nullable=False)
    evidence_locator: Mapped[str | None] = mapped_column(Text)
    evidence_strength: Mapped[str] = mapped_column(String(50), nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_assertion_source_event", "source_id", "source_event_id"),)


class EventVersion(Base):
    __tablename__ = "event_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    canonical_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("event_id", "revision_number", name="uq_event_revision"),)


class EventCurrent(Base):
    __tablename__ = "events_current"

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    canonical_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source_key: Mapped[str] = mapped_column(String(100), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(300), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    exchange: Mapped[str | None] = mapped_column(String(64))
    company_name: Mapped[str | None] = mapped_column(String(300))
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, index=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_timezone: Mapped[str | None] = mapped_column(String(100))
    session: Mapped[str] = mapped_column(String(30), nullable=False)
    fiscal_period: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    time_precision: Mapped[str] = mapped_column(String(30), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    current_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id", name="uq_current_source_event"),
    )


class PublicationManifest(Base):
    __tablename__ = "publication_manifests"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    schema_version: Mapped[str] = mapped_column(String(30), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    freshness_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    horizon_start: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_end: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    jsonl_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    csv_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    included_run_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    universe_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("universe_versions.id"))


class RefreshJob(Base):
    __tablename__ = "refresh_jobs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    action_id: Mapped[UUID] = mapped_column(ForeignKey("operator_actions.id"), nullable=False)
    universe_version_id: Mapped[UUID] = mapped_column(ForeignKey("universe_versions.id"))
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    trigger: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_companies: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_companies: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_ticker: Mapped[str | None] = mapped_column(String(32))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_runs.id"))
    publication_id: Mapped[UUID | None] = mapped_column(ForeignKey("publication_manifests.id"))
    safe_summary: Mapped[str] = mapped_column(Text, default="Waiting for refresh worker.")


class RefreshWorker(Base):
    __tablename__ = "refresh_workers"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_watch: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class SecMetadataCheck(Base):
    """Append-only metadata observation; never an accepted event or feed refresh."""

    __tablename__ = "sec_metadata_checks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    universe_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("universe_versions.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class QuarantineRecord(Base):
    __tablename__ = "quarantine_records"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(300))
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OperatorAction(Base):
    __tablename__ = "operator_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HttpAttempt(Base):
    __tablename__ = "http_attempts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status_class: Mapped[str] = mapped_column(String(30), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EndpointCache(Base):
    __tablename__ = "endpoint_cache"

    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    safe_headers: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)


class SecCompanyCheckpoint(Base):
    __tablename__ = "sec_company_checkpoints"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    universe_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("universe_versions.id"), nullable=False
    )
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    config_key: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    safe_summary: Mapped[str | None] = mapped_column(Text)
    reused_from_id: Mapped[UUID | None] = mapped_column(ForeignKey("sec_company_checkpoints.id"))
    network_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reused_objects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reused_parses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint("run_id", "company_id", name="uq_sec_checkpoint_run_company"),
        Index("ix_sec_checkpoint_resume", "universe_version_id", "company_id", "checked_at"),
    )


class SecDocumentParse(Base):
    """Immutable derived cache; never an accepted assertion or published feed row."""

    __tablename__ = "sec_document_parses"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    evidence_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_for_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)


for _table_name in (
    "event_versions",
    "source_assertions",
    "raw_objects",
    "sec_company_checkpoints",
    "sec_document_parses",
    "sec_metadata_checks",
):
    for _action in ("UPDATE", "DELETE"):
        from sqlalchemy import DDL, event

        event.listen(
            Base.metadata.tables[_table_name],
            "after_create",
            DDL(  # type: ignore[no-untyped-call]
                f"CREATE TRIGGER immutable_{_table_name}_{_action.lower()} BEFORE {_action} "
                f"ON {_table_name} BEGIN SELECT RAISE(ABORT, 'Evidence rows are append-only'); END;"
            ).execute_if(dialect="sqlite"),
        )
