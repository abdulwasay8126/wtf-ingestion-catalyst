from collections.abc import Sequence

from wtf_ingestion.persistence.models import EventCurrent


def without_shadowed_projections(rows: Sequence[EventCurrent]) -> list[EventCurrent]:
    """Next-date forecasts are a fallback, not extra calendar events for announced issuers.

    This is presentation/publication precedence, not guessed event-identity linkage.
    Both independent source assertions and event versions remain in the audit database.
    Callers pass only rows in the same current publication window.
    """
    announced = {
        row.cik
        for row in rows
        if row.event_type == "earnings_release"
        and row.status in {"confirmed", "moved", "cancelled"}
        and row.cik
    }
    return [row for row in rows if row.status != "estimated" or row.cik not in announced]
