"""Recognition regressions grounded in the October 4 retained SEC source audit."""

from datetime import UTC, date, datetime

import pytest

from wtf_ingestion.adapters.earnings_details import conference_call_fact
from wtf_ingestion.adapters.parsing import extract_explicit_announcement
from wtf_ingestion.adapters.sec_native import Filing, parse_exhibit


@pytest.mark.parametrize(
    "ticker,statement,expected,period,session",
    [
        (
            "TSLA",
            "Tesla will post its financial results for the third quarter of 2026 "
            "after market close on Wednesday, October 21, 2026.",
            date(2026, 10, 21),
            "Q3 2026",
            "AMC",
        ),
        (
            "ORLY",
            "The Company announces the release date for its third quarter 2026 results "
            "as Wednesday, October 28, 2026, with a conference call to follow on "
            "Thursday, October 29, 2026.",
            date(2026, 10, 28),
            "Q3 2026",
            "UNKNOWN",
        ),
        (
            "MET",
            "The Company's quarterly earnings release and quarterly financial supplement "
            "for the quarter ended September 30, 2026, scheduled for release on November 4, 2026.",
            date(2026, 11, 4),
            "quarter ended 2026-09-30",
            "UNKNOWN",
        ),
    ],
)
def test_retained_release_forms_preserve_source_period_and_date(
    ticker: str, statement: str, expected: date, period: str, session: str
) -> None:
    filing = Filing("0000000001", "0000000001-26-000001", date(2026, 10, 1), "8-K", "main.htm")
    rows = parse_exhibit(
        f"<html><body><p>{statement}</p></body></html>".encode(),
        filing=filing,
        url=filing.root_url + "ex99.htm",
        ticker=ticker,
        exchange="NASDAQ",
        company_name=ticker,
        observed=datetime(2026, 10, 4, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].event_date == expected and rows[0].fiscal_period == period
    assert rows[0].session.value == session
    assert rows[0].scheduled_start is None and rows[0].source_timezone is None


@pytest.mark.parametrize(
    "statement",
    [
        "Company will post its product demonstration on October 21, 2026.",
        "The conference call for third quarter earnings is scheduled for October 29, 2026.",
        "The investor day is scheduled for December 8, 2026.",
        "Results for the quarter ended September 30, 2026 were released previously.",
        "Company announces the release date for its new product as October 21, 2026.",
        "The Company will post a slide presentation in advance of the earnings call "
        "discussed in the press release to the Investor Relations website.",
    ],
)
def test_other_events_and_period_end_are_not_release_dates(statement: str) -> None:
    with pytest.raises(ValueError, match="no explicit"):
        extract_explicit_announcement(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "Company will post earnings in early November 2026.",
        "The quarterly earnings release is scheduled for release in November.",
        "Company announces the release date for quarterly results as next week.",
    ],
)
def test_unsupported_new_forward_forms_require_review(statement: str) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        extract_explicit_announcement(statement)


def test_conflicting_release_forms_are_not_first_match_wins() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        extract_explicit_announcement(
            "Company will post earnings on October 21, 2026. "
            "The quarterly earnings release is scheduled for release on November 4, 2026."
        )


def test_date_before_posting_clause_is_not_the_reporting_period_end() -> None:
    text = (
        "On July 30, 2026, Weyerhaeuser Company will post and make available on its website "
        "its financial results for the quarter ended June 30, 2026."
    )
    assert extract_explicit_announcement(text)[0] == date(2026, 7, 30)


def test_dated_introduction_cannot_replace_the_explicit_release_date() -> None:
    text = (
        "On October 5, 2026, Company said it will post its financial results "
        "for the third quarter of 2026 on October 21, 2026."
    )
    assert extract_explicit_announcement(text)[0] == date(2026, 10, 21)


@pytest.mark.parametrize(
    "text,release,expected,zone",
    [
        (
            "Company will release earnings on October 28, 2026. "
            "The company will host a webcast beginning at 9:00 a.m. ET that day "
            "to discuss the results.",
            date(2026, 10, 28),
            "2026-10-28T09:00:00-04:00",
            "America/New_York",
        ),
        (
            "Tesla will post third quarter 2026 financial results on October 21, 2026. "
            "At that time, it will issue an advisory linking to the quarterly update. "
            "Management will hold a question and answer webcast that day "
            "at 4:30 p.m. Central Time.",
            date(2026, 10, 21),
            "2026-10-21T16:30:00-05:00",
            "America/Chicago",
        ),
        (
            "Company announces the release date for third quarter 2026 results "
            "as October 28, 2026. "
            "Investors are invited to listen to the conference call discussing the financial "
            "results on Thursday, October 29, 2026, at 10:00 a.m. Central Time.",
            date(2026, 10, 28),
            "2026-10-29T10:00:00-05:00",
            "America/Chicago",
        ),
    ],
)
def test_explicit_linked_standalone_calls_preserve_their_own_clock(
    text: str, release: date, expected: str, zone: str
) -> None:
    result = conference_call_fact(text, release)
    assert result["state"] == "available" and result["at"] == expected
    assert result["timezone"] == zone
    assert extract_explicit_announcement(text)[1] is None


@pytest.mark.parametrize(
    "tail",
    [
        "The company will host a webcast at 9 am ET.",
        "The prior results conference call was on October 29, 2026 at 9 am ET.",
        "The investor day webcast will start that day at 9 am ET.",
        "Its conference call will occur on November 9, 2026 at 9 am ET.",
        "The company will host a webcast that day at 9 am EST.",
        "The company will host a webcast that day at 9 am ET and at 10 am ET.",
    ],
)
def test_unrelated_or_conflicting_standalone_call_is_unavailable(tail: str) -> None:
    result = conference_call_fact(
        "Company will release earnings on October 28, 2026. " + tail, date(2026, 10, 28)
    )
    assert result["state"] == "unavailable" and result["at"] is None
