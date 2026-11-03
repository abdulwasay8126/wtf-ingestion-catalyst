from __future__ import annotations

from datetime import UTC, date, datetime

from icalendar import Calendar

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.domain.enums import EventStatus, EventType, Session, TimePrecision
from wtf_ingestion.domain.models import EventCandidate


class IcsEconomicAdapter:
    parser_version = "official-ics-v1"

    def __init__(self, *, source_id: str, owner: str, source_url: str):
        self.source_id = source_id
        self.owner = owner
        self.source_url = source_url

    def parse(self, raw: bytes, collected_at: datetime | None = None) -> list[EventCandidate]:
        observed = collected_at or datetime.now(UTC)
        try:
            calendar = Calendar.from_ical(raw.decode("utf-8"))
        except Exception as exc:
            raise PayloadError("malformed official ICS calendar") from exc
        results: list[EventCandidate] = []
        for component in calendar.walk("VEVENT"):
            uid = str(component.get("UID", "")).strip()
            summary = str(component.get("SUMMARY", "")).strip()
            start = component.decoded("DTSTART") if component.get("DTSTART") else None
            if not uid or not summary or start is None:
                raise PayloadError("ICS event is missing UID, SUMMARY, or DTSTART")
            event_date: date
            scheduled_start: datetime | None
            precision: TimePrecision
            if isinstance(start, datetime):
                if start.tzinfo is None:
                    raise PayloadError("ICS timestamp must include a timezone")
                scheduled_start = start.astimezone(UTC)
                event_date = scheduled_start.date()
                precision = TimePrecision.EXACT
            else:
                event_date = start
                scheduled_start = None
                precision = TimePrecision.DATE_ONLY
            if event_date < observed.date():
                continue
            url_value = component.get("URL")
            results.append(
                EventCandidate(
                    event_type=EventType.ECONOMIC_RELEASE,
                    company_name=self.owner,
                    title=summary,
                    event_date=event_date,
                    scheduled_start=scheduled_start,
                    session=Session.UNKNOWN,
                    status=EventStatus.CONFIRMED,
                    time_precision=precision,
                    source_id=self.source_id,
                    source_event_id=uid,
                    source_url=str(url_value) if url_value else self.source_url,
                    collected_at=observed,
                    parser_version=self.parser_version,
                    evidence_locator="VEVENT UID/DTSTART/SUMMARY",
                )
            )
        return results
