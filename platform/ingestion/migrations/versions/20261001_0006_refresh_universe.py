"""Fixed listing metadata and audited refresh queue.

Revision ID: 20261001_0006
Revises: 20261001_0005
"""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0006"
down_revision = "20261001_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_runs", sa.Column("universe_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "universe_versions", sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}")
    )
    op.add_column(
        "universe_memberships",
        sa.Column("listings", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "publication_manifests", sa.Column("universe_version_id", sa.Uuid(), nullable=True)
    )
    op.create_table(
        "refresh_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("action_id", sa.Uuid(), sa.ForeignKey("operator_actions.id"), nullable=False),
        sa.Column(
            "universe_version_id", sa.Uuid(), sa.ForeignKey("universe_versions.id"), nullable=False
        ),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("trigger", sa.String(40), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_companies", sa.Integer(), nullable=False),
        sa.Column("total_companies", sa.Integer(), nullable=False),
        sa.Column("current_ticker", sa.String(32), nullable=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("collection_runs.id"), nullable=True),
        sa.Column(
            "publication_id", sa.Uuid(), sa.ForeignKey("publication_manifests.id"), nullable=True
        ),
        sa.Column("safe_summary", sa.Text(), nullable=False),
    )
    op.create_table(
        "refresh_workers",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        # metadata_watch belongs to revision 0010, not this queue revision.
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_run_universe",
            "collection_runs",
            "universe_versions",
            ["universe_version_id"],
            ["id"],
        )
        op.create_foreign_key(
            "fk_publication_universe",
            "publication_manifests",
            "universe_versions",
            ["universe_version_id"],
            ["id"],
        )
        # Preserve the existing typed feed contract; filter it by the currently selected scope.
        op.execute("ALTER VIEW events_feed_current RENAME TO events_feed_horizon")
        op.execute("""CREATE VIEW events_feed_current AS SELECT e.* FROM events_feed_horizon e
            WHERE e.event_type <> 'earnings_release' OR EXISTS (
                SELECT 1 FROM universe_memberships m JOIN companies c ON c.id=m.company_id
                WHERE c.cik=e.cik AND m.universe_version_id=(
                    SELECT id FROM universe_versions
                    WHERE effective_from <= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
                    AND (effective_to IS NULL OR effective_to >=
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date)
                    ORDER BY effective_from DESC, created_at DESC LIMIT 1))""")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP VIEW events_feed_current")
        op.execute("ALTER VIEW events_feed_horizon RENAME TO events_feed_current")
        op.drop_constraint("fk_publication_universe", "publication_manifests", type_="foreignkey")
        op.drop_constraint("fk_run_universe", "collection_runs", type_="foreignkey")
    op.drop_table("refresh_workers")
    op.drop_table("refresh_jobs")
    op.drop_column("publication_manifests", "universe_version_id")
    op.drop_column("universe_memberships", "listings")
    op.drop_column("universe_versions", "provenance")
    op.drop_column("collection_runs", "universe_version_id")
