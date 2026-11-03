"""Optional, source-stated call facts; never substituted for release timing."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from wtf_ingestion.adapters.parsing import (
    ANNOUNCEMENT,
    DATE_PATTERN,
    announcement_date,
    normalize_text,
)

CLOCK = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
ZONE = (
    r"(Eastern\s+Daylight\s+Time|Eastern\s+Standard\s+Time|Eastern\s+Time|"
    r"Pacific\s+Daylight\s+Time|Pacific\s+Standard\s+Time|Pacific\s+Time|"
    r"Central\s+Daylight\s+Time|Central\s+Standard\s+Time|Central\s+Time|"
    r"Mountain\s+Daylight\s+Time|Mountain\s+Standard\s+Time|Mountain\s+Time|"
    r"EDT|EST|ET|PDT|PST|PT|CDT|CST|CT|MDT|MST|MT)\b"
)
CALL_CLOCK = re.compile(rf"(?i)\bat\s+{CLOCK}\s*{ZONE}")


def conference_call_fact(text: str, release_date: date) -> dict[str, Any]:
    normalized = normalize_text(text)
    normalized = re.sub(r"(?i)\ba\.?m\.?", "am", normalized)
    normalized = re.sub(r"(?i)\bp\.?m\.?", "pm", normalized)
    facts = []
    for release in ANNOUNCEMENT.finditer(normalized):
        if announcement_date(release) != release_date:
            continue
        # Bound the relationship to this release and at most three subsequent
        # sentences. Standalone clauses must state a date or an explicit day link.
        tails = re.split(r"[.!?](?=\s|$)", normalized[release.end() : release.end() + 1800])[:4]
        for position, tail in enumerate(tails):
            call = re.search(r"(?i)\b(?:conference\s+call|webcast)\b", tail)
            if call is None or re.search(
                r"(?i)\b(?:prior|previous|replay|investor\s+day|annual\s+meeting)\b", tail
            ):
                continue
            clause = tail[call.start() :]
            dates = list(re.finditer(DATE_PATTERN, clause, flags=re.IGNORECASE))
            relative = re.search(r"(?i)\b(?:that|(?:the\s+)?same|(?:the\s+)?next)\s+day\b", clause)
            if position and not (dates or relative):
                continue
            if len(dates) > 1:
                continue
            day = (
                date_parser.parse(dates[0].group("date")).date()
                if dates
                else release_date + timedelta(days=1)
                if re.search(r"(?i)\b(?:the\s+)?next\s+day\b", clause)
                else release_date
            )
            if not release_date <= day <= release_date + timedelta(days=1):
                continue
            if not dates and re.search(r"(?i)\b(?:later|following\s+week|tomorrow)\b", clause):
                continue
            if not dates and (
                re.search(r"(?i)\bon\s+(?!(?:the\s+)?(?:same|next)\s+day\b)", clause)
                or re.search(r"(?i)\bnext\s+(?!day\b)", clause)
            ):
                continue
            weekday = re.search(
                r"(?i)\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", clause
            )
            if weekday and weekday[1].lower() != day.strftime("%A").lower():
                continue
            for clock in CALL_CLOCK.finditer(clause):
                zone = clock[2].upper()
                prefix = zone[0]
                zone_name = {
                    "E": "America/New_York",
                    "P": "America/Los_Angeles",
                    "C": "America/Chicago",
                    "M": "America/Denver",
                }[prefix]
                local = datetime.combine(
                    day, date_parser.parse(clock[1]).time(), ZoneInfo(zone_name)
                )
                required = (
                    prefix + "DT"
                    if "DAYLIGHT" in zone
                    else prefix + "ST"
                    if "STANDARD" in zone
                    else zone
                    if zone in {"EST", "EDT", "PST", "PDT", "CST", "CDT", "MST", "MDT"}
                    else None
                )
                if required and local.tzname() != required:
                    continue
                facts.append((local.isoformat(), zone_name))
    instants = {datetime.fromisoformat(at).astimezone(UTC) for at, _ in facts}
    if len(instants) != 1:
        return {
            "state": "unavailable",
            "at": None,
            "timezone": None,
            "reason": "No unambiguous dated call clock linked to the forward-release statement.",
        }
    at, timezone = facts[0]
    return {
        "state": "available",
        "at": at,
        "timezone": timezone,
        "precision": "exact",
        "reason": None,
        "evidence_locator": "dated call clause linked to explicit forward-release statement",
    }
