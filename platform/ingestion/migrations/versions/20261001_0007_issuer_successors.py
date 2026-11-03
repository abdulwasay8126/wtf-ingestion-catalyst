"""Preserve successor issuer identities and long-scan progress.

Revision ID: 20261001_0007
Revises: 20261001_0006
"""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0007"
down_revision = "20261001_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("companies") as batch:
        batch.drop_constraint("uq_company_ticker_exchange", type_="unique")
    op.add_column(
        "collection_runs", sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    duplicates = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM (SELECT ticker,exchange FROM companies "
            "GROUP BY ticker,exchange HAVING count(*)>1) t"
        )
    )
    if duplicates:
        raise RuntimeError("Downgrade refused: successor identities must not be deleted or merged.")
    op.drop_column("collection_runs", "last_progress_at")
    with op.batch_alter_table("companies") as batch:
        batch.create_unique_constraint("uq_company_ticker_exchange", ["ticker", "exchange"])
