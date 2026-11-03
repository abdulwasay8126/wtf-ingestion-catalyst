"""Immutable per-company scan checkpoints, separate from accepted feed records."""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0008"
down_revision = "20261001_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sec_company_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("collection_runs.id"), nullable=False),
        sa.Column(
            "universe_version_id", sa.Uuid(), sa.ForeignKey("universe_versions.id"), nullable=False
        ),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("config_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("safe_summary", sa.Text(), nullable=True),
        sa.Column(
            "reused_from_id", sa.Uuid(), sa.ForeignKey("sec_company_checkpoints.id"), nullable=True
        ),
        sa.Column("network_requests", sa.Integer(), nullable=False),
        sa.Column("reused_objects", sa.Integer(), nullable=False),
        # reused_parses is added by revision 0009.
        sa.UniqueConstraint("run_id", "company_id", name="uq_sec_checkpoint_run_company"),
    )
    op.create_index(
        "ix_sec_checkpoint_resume",
        "sec_company_checkpoints",
        ["universe_version_id", "company_id", "checked_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index("ix_raw_endpoint_retrieved", "raw_objects", ["source_url", "retrieved_at"])
        op.execute("""CREATE TRIGGER immutable_sec_company_checkpoints
            BEFORE UPDATE OR DELETE ON sec_company_checkpoints
            FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()""")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_raw_endpoint_retrieved", table_name="raw_objects")
    op.drop_table("sec_company_checkpoints")
