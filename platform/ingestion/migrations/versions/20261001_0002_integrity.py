"""Source-local date and append-only evidence enforcement.

Revision ID: 20261001_0002
Revises: 20261001_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0002"
down_revision = "20261001_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "http_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("collection_runs.id"), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status_class", sa.String(30), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("events_current", sa.Column("source_timezone", sa.String(100), nullable=True))
    op.add_column(
        "publication_manifests",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "publication_manifests",
        sa.Column("freshness_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE publication_manifests SET activated_at = generated_at")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION reject_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Evidence rows are append-only'; END; $$;
        """)
        for table in ("event_versions", "source_assertions", "raw_objects"):
            op.execute(
                f"CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
            )


def downgrade() -> None:
    raise RuntimeError(
        "This audit migration is intentionally forward-only; restore a verified backup instead"
    )
