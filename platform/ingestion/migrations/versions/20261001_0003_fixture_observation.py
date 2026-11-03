"""Correct legacy fixture current-view first-seen clocks; preserve immutable evidence.

Revision ID: 20261001_0003
Revises: 20261001_0002
"""

from alembic import op

revision = "20261001_0003"
down_revision = "20261001_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE events_current
        SET first_seen_at = (
            SELECT min(r.started_at) FROM source_assertions a
            JOIN collection_runs r ON r.id = a.run_id
            WHERE a.event_id = events_current.event_id
        )
        WHERE first_seen_at > (
            SELECT min(r.started_at) FROM source_assertions a
            JOIN collection_runs r ON r.id = a.run_id
            WHERE a.event_id = events_current.event_id
        )
        AND source_id IN (SELECT source_id FROM collection_runs WHERE evidence_mode = 'fixture')
    """)


def downgrade() -> None:
    raise RuntimeError("Forward-only current-view correction; immutable evidence remains unchanged")
