"""Audited lightweight SEC metadata watch."""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0010"
down_revision = "20261002_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {item["name"] for item in sa.inspect(connection).get_columns("refresh_workers")}
    # Preserve compatibility with old 0006 installs that included this later ORM column.
    if "metadata_watch" not in columns:
        op.add_column(
            "refresh_workers",
            sa.Column("metadata_watch", sa.JSON(), nullable=False, server_default="{}"),
        )
    op.create_table(
        "sec_metadata_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column(
            "universe_version_id", sa.Uuid(), sa.ForeignKey("universe_versions.id"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    if connection.dialect.name == "postgresql":
        op.execute("""CREATE TRIGGER immutable_sec_metadata_checks
            BEFORE UPDATE OR DELETE ON sec_metadata_checks
            FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()""")


def downgrade() -> None:
    op.drop_table("sec_metadata_checks")
    op.drop_column("refresh_workers", "metadata_watch")
