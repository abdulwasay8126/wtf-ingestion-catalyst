"""Read-only live feed selection and explicit forecast basis.

Revision ID: 20261001_0005
Revises: 20261001_0004
"""

from alembic import op

revision = "20261001_0005"
down_revision = "20261001_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE VIEW events_feed_current AS
        SELECT '0.2.0'::text AS schema_version,
            e.event_id, e.revision_number, e.event_type, e.ticker, e.exchange,
            e.company_name, e.cik, e.title, e.event_date, e.scheduled_start,
            e.source_timezone, e.session, e.fiscal_period, e.status, e.time_precision,
            e.source_key AS source_id, e.source_event_id, e.source_url,
            e.collected_at, e.first_seen_at, e.last_seen_at, e.content_hash, e.parser_version,
            CASE WHEN e.status='estimated' THEN 'estimated'
                 WHEN e.event_type='economic_release' THEN 'official_schedule'
                 ELSE 'announced' END AS date_basis,
            v.payload->'metadata'->>'estimate_method' AS estimate_method,
            (v.payload->'metadata'->>'estimate_basis_date')::date AS estimate_basis_date,
            v.payload->'metadata'->>'estimate_basis_url' AS estimate_basis_url,
            v.payload->'metadata'->>'estimate_confidence' AS estimate_confidence
        FROM events_current e JOIN event_versions v ON v.id=e.current_version_id
        WHERE e.event_date BETWEEN (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
              AND (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date + 90
          AND (e.status<>'estimated' OR NOT EXISTS (
              SELECT 1 FROM events_current a WHERE a.cik=e.cik
                AND a.event_type='earnings_release'
                AND a.status IN ('confirmed','moved','cancelled')
                AND a.event_date BETWEEN (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
                    AND (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date + 90
          ))
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP VIEW IF EXISTS events_feed_current")
