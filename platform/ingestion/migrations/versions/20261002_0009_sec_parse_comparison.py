"""Checksum/context-bound parsed-document comparison cache."""

import sqlalchemy as sa
from alembic import op

revision = "20261002_0009"
down_revision = "20261001_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # Older installations of 0008 may already contain this later ORM column.
    columns = {
        item["name"] for item in sa.inspect(connection).get_columns("sec_company_checkpoints")
    }
    if "reused_parses" not in columns:
        op.add_column(
            "sec_company_checkpoints",
            sa.Column("reused_parses", sa.Integer(), nullable=False, server_default="0"),
        )
    op.create_table(
        "sec_document_parses",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("collection_runs.id"), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("evidence_mode", sa.String(20), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("parsed_for_date", sa.Date(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    if connection.dialect.name == "postgresql":
        op.execute("""CREATE TRIGGER immutable_sec_document_parses
            BEFORE UPDATE OR DELETE ON sec_document_parses
            FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()""")


def downgrade() -> None:
    op.drop_table("sec_document_parses")
    op.drop_column("sec_company_checkpoints", "reused_parses")
