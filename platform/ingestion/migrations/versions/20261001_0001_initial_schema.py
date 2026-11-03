"""Initial provenance-first ingestion schema.

Revision ID: 20261001_0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(64), nullable=False),
        sa.Column("company_name", sa.String(300), nullable=False),
        sa.Column("cik", sa.String(10), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cik"),
        sa.UniqueConstraint("ticker", "exchange", name="uq_company_ticker_exchange"),
    )
    op.create_index("ix_companies_ticker", "companies", ["ticker"])
    op.create_table(
        "universe_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("assumption", sa.Boolean(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("permission_status", sa.String(50), nullable=False),
        sa.Column("access_method", sa.String(50), nullable=False),
        sa.Column("rate_policy", sa.String(200), nullable=False),
        sa.Column("freshness_hours", sa.Integer(), nullable=False),
        sa.Column("live_enabled", sa.Boolean(), nullable=False),
        sa.Column("fixture_only", sa.Boolean(), nullable=False),
        sa.Column("reviewed_at", sa.Date(), nullable=False),
        sa.Column("next_review_at", sa.Date(), nullable=True),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("limitations", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "source_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("terms_url", sa.Text(), nullable=True),
        sa.Column("robots_url", sa.Text(), nullable=True),
        sa.Column("approval_state", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "universe_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("universe_version_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("coverage_state", sa.String(40), nullable=False),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["universe_version_id"], ["universe_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("universe_version_id", "company_id", name="uq_universe_membership"),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(50), nullable=False),
        sa.Column("evidence_mode", sa.String(20), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("freshness_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_code", sa.String(100), nullable=True),
        sa.Column("safe_error_summary", sa.Text(), nullable=True),
        sa.Column("http_status_class", sa.String(20), nullable=True),
        sa.Column("response_content_type", sa.String(200), nullable=True),
        sa.Column("response_bytes", sa.BigInteger(), nullable=True),
        sa.Column("received_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("revised_count", sa.Integer(), nullable=False),
        sa.Column("anomaly_flags", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "raw_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("retained_until", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raw_objects_sha256", "raw_objects", ["sha256"])
    op.create_table(
        "event_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(64), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "revision_number", name="uq_event_revision"),
    )
    op.create_index("ix_event_versions_event_id", "event_versions", ["event_id"])
    op.create_index("ix_event_versions_canonical_key", "event_versions", ["canonical_key"])
    op.create_table(
        "events_current",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(64), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(100), nullable=False),
        sa.Column("source_event_id", sa.String(300), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("exchange", sa.String(64), nullable=True),
        sa.Column("company_name", sa.String(300), nullable=True),
        sa.Column("cik", sa.String(10), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session", sa.String(30), nullable=False),
        sa.Column("fiscal_period", sa.String(100), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("time_precision", sa.String(30), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_payload_hash", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("canonical_key"),
        sa.UniqueConstraint("source_id", "source_event_id", name="uq_current_source_event"),
    )
    op.create_index("ix_events_current_ticker", "events_current", ["ticker"])
    op.create_index("ix_events_current_cik", "events_current", ["cik"])
    op.create_index("ix_events_current_event_date", "events_current", ["event_date"])
    op.create_table(
        "source_assertions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.String(300), nullable=False),
        sa.Column("evidence_locator", sa.Text(), nullable=True),
        sa.Column("evidence_strength", sa.String(50), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_source_assertions_event_id", "source_assertions", ["event_id"])
    op.create_index(
        "ix_assertion_source_event", "source_assertions", ["source_id", "source_event_id"]
    )
    op.create_table(
        "publication_manifests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(30), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_start", sa.Date(), nullable=False),
        sa.Column("horizon_end", sa.Date(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("jsonl_sha256", sa.String(64), nullable=False),
        sa.Column("csv_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column("included_run_ids", sa.JSON(), nullable=False),
        sa.Column("evidence_mode", sa.String(20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "quarantine_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.String(300), nullable=True),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("safe_summary", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "operator_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("target_id", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )


def downgrade() -> None:
    for table in (
        "operator_actions",
        "quarantine_records",
        "publication_manifests",
        "source_assertions",
        "events_current",
        "event_versions",
        "raw_objects",
        "collection_runs",
        "universe_memberships",
        "source_endpoints",
        "sources",
        "universe_versions",
        "companies",
    ):
        op.drop_table(table)
