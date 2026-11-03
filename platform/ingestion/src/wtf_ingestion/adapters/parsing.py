from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from wtf_ingestion.domain.enums import Session, TimePrecision

MONTH_PATTERN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
DAY_PATTERN = r"\d{1,2}(?:st|nd|rd|th)?"
DATE_PATTERN = (
    rf"(?P<date>{MONTH_PATTERN}\s+{DAY_PATTERN},?\s+20\d{{2}}"
    rf"|{DAY_PATTERN}\s+{MONTH_PATTERN},?\s+20\d{{2}})"
)
WEEKDAY_PATTERN = r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
TIME_PATTERN = r"(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|AM|PM))"
# Clock abbreviations contain periods but do not terminate a source sentence.
SENTENCE_TEXT = r"(?:[ap]\.m\.|[^.!?])"
POST_RESULTS = (
    r"post(?=\s+(?:and\s+make\s+available\s+on\s+(?:its|our|the)\s+website\s+)?"
    r"(?:(?:its|our|the|their)\s+)?"
    r"(?:(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?:fiscal\s+)?20\d{2}\s+"
    r"|(?:fiscal\s+)?20\d{2}\s+(?:Q[1-4]|(?:first|second|third|fourth)\s+quarter)\s+"
    r"|Q[1-4]\s+(?:FY)?20\d{2}\s+)?"
    r"(?:financial\s+results|earnings(?!\s+per\s+share)|results)\b)"
)
LEADING_DATE_PATTERN = DATE_PATTERN.replace("?P<date>", "?P<leading_date>")
UNNAMED_DATE_PATTERN = DATE_PATTERN.replace("(?P<date>", "(?:")
ANNOUNCEMENT = re.compile(
    rf"(?i)(?:(?P<subject>(?:"
    rf"(?:(?:will\s+|plans\s+to\s+|expects\s+to\s+)(?:release|report|announce|{POST_RESULTS})"
    rf"|scheduled\s+to\s+(?:release|report))"
    rf"{SENTENCE_TEXT}{{0,180}}?\b(?:earnings(?!\s+per\s+share)|financial\s+results|results)"
    rf"{SENTENCE_TEXT}{{0,80}}?\bon"
    rf"|(?:quarterly\s+)?earnings\s+release\b{SENTENCE_TEXT}{{0,220}}?"
    rf"\bscheduled\s+for\s+release\s+on"
    rf"|announces?\s+the\s+release\s+date\s+for\b{SENTENCE_TEXT}{{0,180}}?"
    rf"\b(?:financial\s+results|results|earnings)\b{SENTENCE_TEXT}{{0,40}}?\bas"
    rf"))\s+{WEEKDAY_PATTERN}{DATE_PATTERN}"
    rf"(?:\s*,?\s+at\s+{TIME_PATTERN}\s*(?P<zone>ET|EST|EDT|PT|PST|PDT)\b)?"
    rf"|\bOn\s+{WEEKDAY_PATTERN}{LEADING_DATE_PATTERN},?\s+"
    rf"{SENTENCE_TEXT}{{0,100}}?\bwill\s+{POST_RESULTS}"
    # A dated introduction cannot override the explicitly dated results clause.
    rf"(?!{SENTENCE_TEXT}{{0,280}}?\bon\s+{WEEKDAY_PATTERN}{UNNAMED_DATE_PATTERN})"
    rf"{SENTENCE_TEXT}{{0,180}})"
)


def announcement_date(match: re.Match[str]) -> date:
    return date_parser.parse(match.group("date") or match.group("leading_date"), fuzzy=False).date()


def normalize_text(text: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", text)
        .translate({0x00AD: None, 0x2011: "-", 0x2013: "-", 0x2014: "-"})
        .split()
    )


def extract_explicit_announcement(
    text: str,
) -> tuple[date, datetime | None, Session, TimePrecision]:
    normalized = normalize_text(text)
    matches = list(ANNOUNCEMENT.finditer(normalized))
    if not matches:
        if re.search(
            rf"(?i)\b(?:will|plans\s+to|expects\s+to|scheduled\s+to)\s+"
            rf"(?:release|report|announce|{POST_RESULTS})[^.!?]{{0,220}}"
            rf"\b(?:earnings(?!\s+per\s+share)|financial\s+results)\b",
            normalized,
        ) or re.search(
            rf"(?i)\b(?:earnings\s+release\b{SENTENCE_TEXT}{{0,220}}?"
            rf"\bscheduled\s+for\s+release|announces?\s+the\s+release\s+date\s+for\b"
            rf"{SENTENCE_TEXT}{{0,180}}?\b(?:earnings|financial\s+results|results)\b)",
            normalized,
        ):
            raise ValueError("unsupported forward earnings announcement requires review")
        raise ValueError("no explicit forward earnings announcement found")
    dates = [announcement_date(item) for item in matches]
    if len(set(dates)) > 1:
        raise ValueError("ambiguous multiple earnings dates")
    clocks = {(item.group("time"), item.group("zone")) for item in matches if item.group("time")}
    if len(clocks) > 1:
        raise ValueError("ambiguous multiple earnings times")
    match = matches[0]
    event_date = dates[0]
    tail = re.split(r"[.!?](?=\s|$)", normalized[match.end() :], maxsplit=1)[0]
    lowered = (normalized[match.start() : match.end()] + tail).lower()
    lowered = re.split(r"\b(?:conference\s+call|webcast)\b", lowered, maxsplit=1)[0]
    session = Session.UNKNOWN
    if any(
        value in lowered
        for value in (
            "after the market close",
            "after market close",
            "following market close",
            "after the close of the market",
        )
    ):
        session = Session.AMC
    elif any(
        value in lowered
        for value in (
            "before the market open",
            "before market open",
            "prior to market open",
            "before the opening of the market",
        )
    ):
        session = Session.BMO
    if not match.group("time"):
        return event_date, None, session, TimePrecision.DATE_ONLY
    zone = match.group("zone").upper()
    zone_name = "America/New_York" if zone.startswith("E") else "America/Los_Angeles"
    parsed_time = date_parser.parse(match.group("time")).time()
    local = datetime.combine(event_date, parsed_time, tzinfo=ZoneInfo(zone_name))
    if zone not in {"ET", "PT"} and local.tzname() != zone:
        raise ValueError("ambiguous timezone abbreviation for the stated date")
    return event_date, local, session, TimePrecision.EXACT
