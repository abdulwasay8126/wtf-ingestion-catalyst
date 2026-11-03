from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wtf_ingestion.adapters.base import PayloadError, SourcePermissionError, UnreadableDocumentError
from wtf_ingestion.adapters.parsing import extract_explicit_announcement
from wtf_ingestion.adapters.sec_native import parse_document_index, parse_exhibit, parse_submissions

OBSERVED = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "body",
    [
        b"<HTML><BODY><p>We will release earnings on October 29, 2026.</p></BODY>",
        b"<p>We will release earnings on October 29, 2026.</p>",
    ],
)
def test_complete_sec_text_wrapper_allows_html_fragments(fixture_root: Path, body: bytes) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    raw = b"<DOCUMENT><TYPE>EX-99.1<TEXT>" + body + b"</TEXT></DOCUMENT>"
    events = parse_exhibit(
        raw,
        filing=filing,
        url=filing.root_url + "ex991.htm",
        ticker="AAPL",
        exchange="NASDAQ",
        company_name="Apple",
        observed=OBSERVED,
    )
    assert len(events) == 1 and events[0].event_date.isoformat() == "2026-10-29"


@pytest.mark.parametrize("tail", [b"", b"</TEXT>", b"</DOCUMENT>"])
def test_truncated_sec_wrapper_still_fails(fixture_root: Path, tail: bytes) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    with pytest.raises(PayloadError, match="complete TEXT"):
        parse_exhibit(
            b"<DOCUMENT><TEXT><p>We will release earnings on October 29, 2026.</p>" + tail,
            filing=filing,
            url=filing.root_url + "ex991.htm",
            ticker="AAPL",
            exchange="NASDAQ",
            company_name="Apple",
            observed=OBSERVED,
        )


def test_complete_image_only_sec_wrapper_is_not_a_valid_empty(fixture_root: Path) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    with pytest.raises(UnreadableDocumentError):
        parse_exhibit(
            b"<DOCUMENT><DESCRIPTION>Long metadata is not event evidence"
            b"<TEXT><HTML><BODY><img src='page.jpg'></BODY></TEXT></DOCUMENT>",
            filing=filing,
            url=filing.root_url + "ex991.htm",
            ticker="AAPL",
            exchange="NASDAQ",
            company_name="Apple",
            observed=OBSERVED,
        )


def test_native_sec_boundaries(fixture_root: Path) -> None:
    filings = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )
    assert len(filings) == 1
    urls = parse_document_index((fixture_root / "sec/document_index.html").read_bytes(), filings[0])
    assert len(urls) == 2
    events = parse_exhibit(
        (fixture_root / "sec/exhibit_native.html").read_bytes(),
        filing=filings[0],
        url=urls[1],
        ticker="AAPL",
        exchange="NASDAQ",
        company_name="Apple Inc.",
        observed=OBSERVED,
    )
    assert len(events) == 1
    assert events[0].fiscal_period == "Q4 FY2026"
    assert events[0].metadata["accession_number"] == filings[0].accession


@pytest.mark.parametrize("fault", ["identity", "partial", "malformed", "future", "path"])
def test_native_submissions_fail_closed(fixture_root: Path, fault: str) -> None:
    raw = (fixture_root / "sec/submissions_native.json").read_bytes()
    payload = json.loads(raw)
    if fault == "identity":
        payload["cik"] = "789019"
    elif fault == "partial":
        payload["filings"]["recent"]["form"].pop()
    elif fault == "malformed":
        payload["filings"]["recent"] = []
    elif fault == "future":
        payload["filings"]["recent"]["filingDate"][0] = "2030-01-01"
    else:
        payload["filings"]["recent"]["primaryDocument"][0] = "../escape.htm"
    with pytest.raises(PayloadError):
        parse_submissions(json.dumps(payload).encode(), "320193", OBSERVED)


def test_document_index_cannot_escape(fixture_root: Path) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    raw = (
        (fixture_root / "sec/document_index.html")
        .read_bytes()
        .replace(b"/Archives/edgar/data/", b"https://evil.invalid/Archives/edgar/data/")
    )
    with pytest.raises(SourcePermissionError):
        parse_document_index(raw, filing)


@pytest.mark.parametrize(
    "text",
    [
        "We will release a product on October 29, 2026.",
        "We furnished quarterly results on October 29, 2026.",
        "We will release earnings on October 29, 2026. "
        "We will report financial results on November 5, 2026.",
    ],
)
def test_only_unambiguous_forward_results_sentence(text: str) -> None:
    with pytest.raises(ValueError):
        extract_explicit_announcement(text)


@pytest.mark.parametrize(
    ("announcement", "expected"),
    [
        ("We will release earnings on October 29, 2026.", None),
        (
            "We will release second quarter fiscal 2026 results on October 29, 2026.",
            "Q2 FY2026",
        ),
        (
            "We will release financial results for the third quarter of 2026 "
            "before the market opens on October 29, 2026.",
            "Q3 2026",
        ),
        (
            "We will release financial results for the third quarter of fiscal 2026 "
            "on October 29, 2026.",
            "Q3 FY2026",
        ),
    ],
)
def test_fiscal_period_is_not_borrowed_from_old_results(
    fixture_root: Path, announcement: str, expected: str | None
) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    events = parse_exhibit(
        (
            "<p>First quarter fiscal 2025 results were previously reported.</p>"
            f"<p>{announcement}</p>"
        ).encode(),
        filing=filing,
        url=filing.root_url + "ex99-1.htm",
        ticker="AAPL",
        exchange="NASDAQ",
        company_name="Apple Inc.",
        observed=OBSERVED,
    )
    assert len(events) == 1 and events[0].fiscal_period == expected


def test_conflicting_periods_in_explicit_announcements_fail(fixture_root: Path) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    with pytest.raises(PayloadError, match="fiscal periods"):
        parse_exhibit(
            b"<p>We will release first quarter fiscal 2026 results on October 29, 2026. "
            b"We will release second quarter fiscal 2026 results on October 29, 2026.</p>",
            filing=filing,
            url=filing.root_url + "ex99-1.htm",
            ticker="AAPL",
            exchange="NASDAQ",
            company_name="Apple Inc.",
            observed=OBSERVED,
        )


def test_period_before_announcement_is_not_silently_attached(fixture_root: Path) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    events = parse_exhibit(
        b"<p>Results for the third quarter of 2026 were filed earlier. "
        b"We will release financial results on October 29, 2026.</p>",
        filing=filing,
        url=filing.root_url + "ex99-1.htm",
        ticker="AAPL",
        exchange="NASDAQ",
        company_name="Apple",
        observed=OBSERVED,
    )
    assert events[0].fiscal_period is None


@pytest.mark.parametrize("fault", [None, "missing_target", "undeclared_cofiler", "other_accession"])
def test_joint_filing_requires_verified_cofilers_and_accession(
    fixture_root: Path, fault: str | None
) -> None:
    filing = parse_submissions(
        (fixture_root / "sec/submissions_native.json").read_bytes(), "320193", OBSERVED
    )[0]
    raw = (
        (fixture_root / "sec/document_index.html")
        .read_bytes()
        .replace(b"/data/320193/", b"/data/7323/")
    )
    target = b'<div class="companyName">Example parent (Filer) CIK : 0000320193</div>'
    cofiler = b'<div class="companyName">Example subsidiary (Filer) CIK : 0000007323</div>'
    if fault == "missing_target":
        target = b""
    elif fault == "undeclared_cofiler":
        cofiler = b""
    elif fault == "other_accession":
        raw = raw.replace(b"000032019326000099", b"000032019326000123")
    raw += target + cofiler
    if fault is not None:
        with pytest.raises(SourcePermissionError):
            parse_document_index(raw, filing)
    else:
        urls = parse_document_index(raw, filing)
        assert len(urls) == 2 and all("/data/7323/" in url for url in urls)
        assert urls[0].endswith(filing.primary_document)
