from datetime import date

import pytest

from wtf_ingestion.adapters.earnings_details import conference_call_fact


@pytest.mark.parametrize(
    "clause,expected",
    [
        ("and will hold a conference call at 10:00 a.m. Eastern Time", "2026-10-27T10:00:00-04:00"),
        (
            "and will hold a conference call the next day at 11:00 a.m. Eastern Daylight Time",
            "2026-10-28T11:00:00-04:00",
        ),
        ("with a conference call on October 28, 2026 at 9 a.m. ET", "2026-10-28T09:00:00-04:00"),
    ],
)
def test_source_stated_call_clock_stays_separate(clause: str, expected: str) -> None:
    text = "Company will release third quarter 2026 earnings on October 27, 2026 " + clause + "."
    call = conference_call_fact(text, date(2026, 10, 27))
    assert call["at"] == expected and call["state"] == "available"


@pytest.mark.parametrize(
    "text",
    [
        "Company will release earnings on October 27, 2026. Prior results call at 8 a.m. ET.",
        "Company will release earnings on October 27, 2026 "
        "and host a conference call later at 9 am ET.",
        "Company will release earnings on October 27, 2026 "
        "and host a conference call next morning at 9 am ET.",
        "Company will release earnings on October 27, 2026 "
        "and host a conference call on October 28 at 9 am ET.",
        "Company will release earnings on October 27, 2026 "
        "and host a conference call Friday at 9 am ET.",
        "Company will release earnings on October 27, 2026 and host a conference call at 9 am EST.",
        "Prior call at 9 am ET. Company will release earnings on October 27, 2026.",
    ],
)
def test_unsupported_or_unrelated_call_clocks_are_not_borrowed(text: str) -> None:
    call = conference_call_fact(text, date(2026, 10, 27))
    assert call["state"] == "unavailable" and call["at"] is None
