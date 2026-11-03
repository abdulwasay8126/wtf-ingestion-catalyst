"""Validated SEC conditional-request cache.

Revision ID: 20261001_0004
Revises: 20261001_0003
"""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0004"
down_revision = "20261001_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "endpoint_cache",
        sa.Column("url_hash", sa.String(64), primary_key=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("safe_headers", sa.JSON(), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("endpoint_cache")
