from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from wtf_ingestion.adapters.parsing import extract_explicit_announcement
from wtf_ingestion.adapters.sec_native import Filing, parse_exhibit


@pytest.mark.parametrize("verb", ["release", "report", "announce"])
def test_explicit_plans_to_announce_results(verb: str) -> None:
    day, start, session, precision = extract_explicit_announcement(
        f"Company plans to {verb} third quarter 2026 earnings "
        "after the market closes on October 19, 2026, and will hold a conference call "
        "the next day at 11:00 a.m. ET."
    )
    assert day == date(2026, 10, 19) and start is None
    assert session.value == "AMC" and precision.value == "date_only"


@pytest.mark.parametrize(
    ("ticker", "cik", "accession", "document", "statement", "expected_date", "period", "session"),
    [
        (
            "DHI",
            "0000882184",
            "0000882184-26-000092",
            "a6302026exhibit991.htm",
            "The Company plans to release financial results for its fourth quarter and fiscal "
            "year 2026 on Thursday, October 29, 2026 before the market opens, with a conference "
            "call at 8:30 a.m. Eastern Time.",
            date(2026, 10, 29),
            "Q4 and FY2026",
            "BMO",
        ),
        (
            "STLD",
            "0001022671",
            "0001104659-26-108777",
            "tm2625690d1_ex99-1.htm",
            "The company plans to release its third quarter 2026 earnings after the market "
            "closes on October 19, 2026, and will hold a conference call the next day at "
            "11:00 a.m. Eastern Daylight Time.",
            date(2026, 10, 19),
            "Q3 2026",
            "AMC",
        ),
        (
            "FCX",
            "0000831259",
            "0000831259-26-000043",
            "fcx_261002.htm",
            "FCX plans to release its third-quarter 2026 earnings results before the market "
            "opens on Tuesday, October 27, 2026, and will hold a conference call to discuss "
            "the results at 10:00 a.m. Eastern Time.",
            date(2026, 10, 27),
            "Q3 2026",
            "BMO",
        ),
    ],
)
def test_retained_sec_planned_release_periods_and_separate_call_times(
    ticker: str,
    cik: str,
    accession: str,
    document: str,
    statement: str,
    expected_date: date,
    period: str,
    session: str,
) -> None:
    # Public source excerpts, without contacts or unrelated financial values:
    # DHI: https://www.sec.gov/Archives/edgar/data/882184/000088218426000092/
    #      a6302026exhibit991.htm (retained SHA-256 94468bc6056ce41d4ba7ff56283200dfb
    #      b9d64d78f3cf7b90db199ae3f35b5af).
    # STLD: https://www.sec.gov/Archives/edgar/data/1022671/000110465926108777/
    #       tm2625690d1_ex99-1.htm (retained SHA-256 ee613cb8d53a5bd3fefce8b10a65b9b5
    #       44328d519644a6bbc90cc7a2b1acde2e).
    # FCX: https://www.sec.gov/Archives/edgar/data/831259/000083125926000043/
    #      fcx_261002.htm (retained SHA-256 f15ce51c7e6470ce9245d7157098d7519
    #      db5a5e002e7fb5f2f3649f2a1c07b51).
    filing = Filing(cik, accession, date(2026, 7, 1), "8-K", "primary.htm")
    events = parse_exhibit(
        f"<html><body><p>{statement}</p></body></html>".encode(),
        filing=filing,
        url=filing.root_url + document,
        ticker=ticker,
        exchange="NASDAQ" if ticker == "STLD" else "NYSE",
        company_name=ticker,
        observed=datetime(2026, 10, 2, tzinfo=UTC),
    )
    assert len(events) == 1
    event = events[0]
    assert event.event_date == expected_date
    assert event.fiscal_period == period and event.session.value == session
    assert event.scheduled_start is None and event.source_timezone is None
    assert event.time_precision.value == "date_only" and event.parser_version == "sec-native-v15"
    assert str(event.source_url) == filing.root_url + document


def test_future_reporting_period_end_is_not_a_release_date() -> None:
    # Retained FDX 8-K: https://www.sec.gov/Archives/edgar/data/1048911/
    # 000104891126000108/fdx-20260721.htm; the dates describe the reporting period.
    with pytest.raises(ValueError, match="no explicit"):
        extract_explicit_announcement(
            "The Company will report operating results covering the seven-month transition "
            "period from June 1, 2026 through December 31, 2026 in a Transition Report "
            "on Form 10-K."
        )


def test_planned_results_release_in_past_is_not_a_future_event() -> None:
    filing = Filing("0001022671", "0001104659-26-108777", date(2026, 7, 1), "8-K", "primary.htm")
    assert not parse_exhibit(
        b"<p>Company plans to release third quarter 2026 earnings on October 19, 2026.</p>",
        filing=filing,
        url=filing.root_url + "ex99-1.htm",
        ticker="STLD",
        exchange="NASDAQ",
        company_name="Steel Dynamics",
        observed=datetime(2026, 10, 20, tzinfo=UTC),
    )


def test_planned_non_earnings_release_is_not_an_event() -> None:
    with pytest.raises(ValueError, match="no explicit"):
        extract_explicit_announcement("Company plans to release a product on October 19, 2026.")


@pytest.mark.parametrize("day", ["Oct. 29th, 2026", "29 October 2026", "October 29 2026"])
def test_explicit_date_formats_preserve_event_facts(day: str) -> None:
    event_date, start, session, precision = extract_explicit_announcement(
        f"Company will report third-quarter 2026 earnings on {day} prior to market open."
    )
    assert event_date == date(2026, 10, 29) and session.value == "BMO"
    assert start is None and precision.value == "date_only"


def test_unrecognized_forward_statement_is_not_silent_empty() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        extract_explicit_announcement("Company will report earnings in early November 2026.")


def test_expected_past_release_is_not_a_future_gaap_statement() -> None:
    # Source-grounded SPGI wording: the later GAAP sentence refers to the dated July release.
    text = (
        "The Company expects to report 2Q 2026 results on July 28, 2026, at which time "
        "the Company will report GAAP financial results that will include Mobility "
        "for the full second quarter."
    )
    assert extract_explicit_announcement(text)[0] == date(2026, 7, 28)
    filing = Filing("0000064040", "0001104659-26-080571", date(2026, 7, 1), "8-K", "a.htm")
    assert (
        parse_exhibit(
            f"<p>{text}</p>".encode(),
            filing=filing,
            url=filing.root_url + "ex99-1.htm",
            ticker="SPGI",
            exchange="NYSE",
            company_name="S&P Global",
            observed=datetime(2026, 10, 3, tzinfo=UTC),
        )
        == []
    )


def test_revenue_eps_guidance_is_not_an_earnings_date_statement() -> None:
    with pytest.raises(ValueError, match="no explicit"):
        extract_explicit_announcement(
            "Company expects to report revenue between $100 and $120 million, "
            "and non-GAAP earnings per share of $2.00 to $2.10 for 2026."
        )


def test_reporting_basis_is_not_linked_to_the_next_sentence_results() -> None:
    with pytest.raises(ValueError, match="no explicit"):
        extract_explicit_announcement(
            "Company will report operating results on a calendar-year basis starting in 2027. "
            "Previously it reported financial results on a fiscal-year basis."
        )


def test_weekday_and_explicit_eastern_time() -> None:
    day, start, session, precision = extract_explicit_announcement(
        "Company will report quarterly earnings on Thursday, October 29, 2026 "
        "at 4:30 p.m. ET after market close."
    )
    assert day == date(2026, 10, 29) and start is not None
    assert session.value == "AMC" and precision.value == "exact"


def test_next_sentence_conference_call_time_is_not_release_time() -> None:
    day, start, _, precision = extract_explicit_announcement(
        "Company will release earnings on October 29, 2026. "
        "Its conference call is on October 30, 2026 at 9 a.m. ET."
    )
    assert day == date(2026, 10, 29) and start is None and precision.value == "date_only"


@pytest.mark.parametrize(
    ("period_text", "expected"),
    [
        ("fiscal 2027 first quarter", "Q1 FY2027"),
        ("fiscal year 2027 second-quarter", "Q2 FY2027"),
        ("FY2027 Q3", "Q3 FY2027"),
        ("2027 fourth quarter", "Q4 2027"),
        ("2027 Q1", "Q1 2027"),
    ],
)
def test_explicit_year_before_quarter(period_text: str, expected: str) -> None:
    # TPR's retained SEC statement uses the first ordering, not a guessed calendar quarter.
    filing = Filing("0001116132", "0001140361-26-032624", date(2026, 8, 1), "8-K", "a.htm")
    rows = parse_exhibit(
        f"<p>The Company expects to report {period_text} results on November 5, 2026.</p>".encode(),
        filing=filing,
        url=filing.root_url + "ex99-1.htm",
        ticker="TPR",
        exchange="NYSE",
        company_name="Tapestry",
        observed=datetime(2026, 10, 3, tzinfo=UTC),
    )
    assert rows[0].fiscal_period == expected
    assert rows[0].session.value == "UNKNOWN" and rows[0].scheduled_start is None


def test_reversed_period_in_previous_sentence_is_not_borrowed() -> None:
    filing = Filing("0001116132", "0001140361-26-032624", date(2026, 8, 1), "8-K", "a.htm")
    rows = parse_exhibit(
        b"<p>Fiscal 2027 first quarter outlook. "
        b"Company will report results on November 5, 2026.</p>",
        filing=filing,
        url=filing.root_url + "ex99-1.htm",
        ticker="TPR",
        exchange="NYSE",
        company_name="Tapestry",
        observed=datetime(2026, 10, 3, tzinfo=UTC),
    )
    assert rows[0].fiscal_period is None


def test_conflicting_year_before_quarter_requires_review() -> None:
    from wtf_ingestion.adapters.base import PayloadError

    filing = Filing("0001116132", "0001140361-26-032624", date(2026, 8, 1), "8-K", "a.htm")
    with pytest.raises(PayloadError, match="fiscal periods"):
        parse_exhibit(
            b"<p>Company will report fiscal 2027 first quarter results on November 5, 2026. "
            b"Company will report fiscal 2027 second quarter results on November 5, 2026.</p>",
            filing=filing,
            url=filing.root_url + "ex99-1.htm",
            ticker="TPR",
            exchange="NYSE",
            company_name="Tapestry",
            observed=datetime(2026, 10, 3, tzinfo=UTC),
        )


def test_same_sentence_call_time_is_not_release_time() -> None:
    day, start, session, precision = extract_explicit_announcement(
        "Company will release earnings on October 29, 2026 and host a conference call "
        "at 9 a.m. ET before market open."
    )
    assert day == date(2026, 10, 29) and start is None
    assert session.value == "UNKNOWN" and precision.value == "date_only"


def test_session_from_next_sentence_is_not_borrowed() -> None:
    _, _, session, _ = extract_explicit_announcement(
        "Company will release earnings on October 29, 2026. "
        "Its conference call will begin before market open."
    )
    assert session.value == "UNKNOWN"


def test_conflicting_explicit_release_times_fail() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        extract_explicit_announcement(
            "Company will release earnings on October 29, 2026 at 4 p.m. ET. "
            "Company will release earnings on October 29, 2026 at 5 p.m. ET."
        )


def test_dst_conflicting_abbreviation_is_not_silently_repaired() -> None:
    with pytest.raises(ValueError, match="ambiguous timezone"):
        extract_explicit_announcement(
            "Company will release earnings on October 29, 2026 at 4 p.m. EST."
        )
