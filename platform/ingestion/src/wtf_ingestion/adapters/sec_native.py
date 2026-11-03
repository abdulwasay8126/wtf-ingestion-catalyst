from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

from dateutil import parser as date_parser
from selectolax.parser import HTMLParser

from wtf_ingestion.adapters.base import PayloadError, SourcePermissionError, UnreadableDocumentError
from wtf_ingestion.adapters.earnings_details import conference_call_fact
from wtf_ingestion.adapters.parsing import (
    ANNOUNCEMENT,
    DATE_PATTERN,
    extract_explicit_announcement,
    normalize_text,
)
from wtf_ingestion.adapters.pdf_text import extract_pdf_text
from wtf_ingestion.domain.enums import EventStatus, EventType
from wtf_ingestion.domain.models import EventCandidate

ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
DOCUMENT = re.compile(r"^[A-Za-z0-9_.-]+\.(?:htm|html|txt)$", re.IGNORECASE)
EXHIBIT_DOCUMENT = re.compile(r"^[A-Za-z0-9_.-]+\.(?:htm|html|txt|pdf)$", re.IGNORECASE)
NATIVE_PARSER_VERSION = "sec-native-v15"


@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str
    filed_on: date
    form: str
    primary_document: str
    items: str = ""

    @property
    def root_url(self) -> str:
        directory = self.accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/{directory}/"

    @property
    def index_url(self) -> str:
        return self.root_url + self.accession + "-index.html"


def parse_submissions(
    raw: bytes, expected_cik: str, observed: datetime, *, lookback_days: int = 120
) -> list[Filing]:
    """Validate actual SEC parallel arrays; never accept a silently truncated recent window."""
    try:
        payload: dict[str, Any] = json.loads(raw)
        if str(payload["cik"]).zfill(10) != expected_cik.zfill(10):
            raise PayloadError("SEC identity does not match requested company")
        recent = payload["filings"]["recent"]
        fields = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")
        if not all(isinstance(recent[key], list) for key in fields):
            raise PayloadError("SEC recent fields must be arrays")
        if len({len(recent[key]) for key in fields}) != 1:
            raise PayloadError("SEC parallel arrays have inconsistent lengths")
        cutoff = observed.date() - timedelta(days=lookback_days)
        dates = [date.fromisoformat(value) for value in recent["filingDate"]]
        if len(dates) >= 1000 and min(dates) > cutoff:
            raise PayloadError("recent filing window is incomplete; archive paging is required")
        if any(value > observed.date() for value in dates):
            raise PayloadError("SEC filing date is in the future")
        filings = []
        for index, filed_on in enumerate(dates):
            form = recent["form"][index]
            if form not in {"8-K", "8-K/A"} or filed_on < cutoff:
                continue
            accession = recent["accessionNumber"][index]
            document = recent["primaryDocument"][index]
            if not ACCESSION.fullmatch(accession) or not DOCUMENT.fullmatch(document):
                raise PayloadError("SEC filing identifier or primary document is malformed")
            items = recent["items"][index]
            if not isinstance(items, str):
                raise PayloadError("SEC filing items must be a string")
            filings.append(
                Filing(expected_cik.zfill(10), accession, filed_on, form, document, items)
            )
        return filings
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PayloadError("malformed SEC submissions structure") from exc


def parse_document_index(raw: bytes, filing: Filing) -> list[str]:
    tree = HTMLParser(raw)
    table = tree.css_first('table[summary="Document Format Files"]')
    if table is None:
        raise PayloadError("SEC document table is missing or changed")
    cofilers = set()
    for company in tree.css(".companyName"):
        match = re.search(r"\bCIK\s*:\s*(\d{10})\b", company.text(separator=" ", strip=True))
        if match:
            cofilers.add(int(match[1]))
    primary_urls = set()
    exhibits = []
    for row in table.css("tr"):
        cells = row.css("td")
        if not cells:
            continue
        if len(cells) < 4:
            raise PayloadError("SEC document row is incomplete")
        link = cells[2].css_first("a")
        kind = cells[3].text(strip=True).upper()
        if not kind.startswith("EX-99") and kind not in {"8-K", "8-K/A"}:
            continue  # Not an earnings evidence document; never fetch this link.
        if link is None:
            raise PayloadError("SEC document link is missing")
        href = link.attributes.get("href") or ""
        # SEC's iXBRL viewer wraps the same archive document in a ?doc= URL.
        if href.startswith("/ix?doc=") or href.startswith("/ixviewer/doc/action?doc="):
            href = href.split("?doc=", 1)[1]
        url = urljoin(filing.index_url, href)
        parsed = urlsplit(url)
        archive = re.fullmatch(
            r"/Archives/edgar/data/(\d{1,10})/(\d{18})/([A-Za-z0-9_.-]+)", parsed.path
        )
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.sec.gov"
            or archive is None
            or archive[2] != filing.accession.replace("-", "")
            or (
                int(archive[1]) != int(filing.cik)
                and not (int(filing.cik) in cofilers and int(archive[1]) in cofilers)
            )
            or parsed.query
            or parsed.fragment
        ):
            raise SourcePermissionError("SEC document is outside its verified filing/registrants")
        name = parsed.path.rsplit("/", 1)[-1]
        if not EXHIBIT_DOCUMENT.fullmatch(name) or parsed.query:
            if kind.startswith("EX-99") or kind in {"8-K", "8-K/A"}:
                raise PayloadError("unsupported filing document format")
            continue
        if name == filing.primary_document and kind == filing.form:
            primary_urls.add(url)
        if kind.startswith("EX-99"):
            exhibits.append(url)
    if len(primary_urls) != 1:
        raise PayloadError("primary filing document is absent from the document index")
    primary = next(iter(primary_urls))
    canonical_root = primary.rsplit("/", 1)[0] + "/"
    if any(not exhibit.startswith(canonical_root) for exhibit in exhibits):
        raise SourcePermissionError("SEC exhibits leave the verified primary document directory")
    return list(dict.fromkeys([primary, *exhibits]))


def filing_document_text(raw: bytes, url: str, extracted_text: str | None = None) -> str:
    """Use the same bounded evidence text for calendar parsing and research packs."""
    is_pdf = urlsplit(url).path.lower().endswith(".pdf")
    if extracted_text is not None:
        text = extracted_text
    elif is_pdf:
        text = extract_pdf_text(raw)
    else:
        if raw.startswith(b"%PDF-"):
            raise PayloadError("SEC filing format does not match its document path")
        html = raw
        if raw.lstrip().upper().startswith(b"<DOCUMENT>"):
            wrapped = re.search(rb"(?is)<TEXT\s*>(.*?)</TEXT\s*>\s*</DOCUMENT\s*>\s*$", raw)
            if wrapped is None:
                raise PayloadError("SEC SGML wrapper lacks a complete TEXT document")
            # EDGAR also serves HTML fragments with optional end tags. Require the
            # enclosing SEC TEXT/DOCUMENT boundaries, not an optional HTML end tag.
            html = wrapped[1]
        tree = HTMLParser(html)
        for node in tree.css("script, style"):
            node.decompose()
        # SEC may wrap HTML in SGML DOCUMENT/TEXT tags. Metadata and TITLE text
        # are not evidence that an image-only exhibit has a readable body.
        body = tree.body
        text = body.text(separator=" ", strip=True) if body is not None else ""
    if len(text.strip()) < 20:
        raise UnreadableDocumentError("SEC filing body text is missing or incomplete")
    return text


def parse_exhibit(
    raw: bytes,
    *,
    filing: Filing,
    url: str,
    ticker: str,
    exchange: str,
    company_name: str,
    observed: datetime,
    extracted_text: str | None = None,
) -> list[EventCandidate]:
    text = filing_document_text(raw, url, extracted_text)
    is_pdf = urlsplit(url).path.lower().endswith(".pdf")
    try:
        event_date, start, session, precision = extract_explicit_announcement(text)
    except ValueError as exc:
        if "ambiguous" in str(exc) or "unsupported" in str(exc):
            raise PayloadError("forward earnings statement needs explicit parser review") from exc
        return []
    if event_date < observed.date():
        return []
    periods = set()
    # Never borrow a quarter from a title, a prior-period result, or another sentence.
    for statement in ANNOUNCEMENT.finditer(normalize_text(text)):
        for fiscal in re.finditer(
            r"(?i)\b(?:(fiscal(?:\s+year)?|FY)\s*)?(20\d{2})\s+"
            r"(?:(first|second|third|fourth)(?:\s+|\s*-\s*)quarter|Q([1-4]))\b",
            statement.group(0),
        ):
            quarter = (
                {"first": 1, "second": 2, "third": 3, "fourth": 4}[fiscal[3].lower()]
                if fiscal[3]
                else int(fiscal[4])
            )
            periods.add(f"Q{quarter} {'FY' if fiscal[1] else ''}{fiscal[2]}")
        for fiscal in re.finditer(
            r"(?i)\b(first|second|third|fourth)(?:\s+|\s*-\s*)quarter\s+"
            r"(?:(and\s+fiscal\s+year)\s+|(?:of\s+)?(fiscal\s+)?)(20\d{2})\b",
            statement.group(0),
        ):
            quarter = {"first": 1, "second": 2, "third": 3, "fourth": 4}[fiscal[1].lower()]
            # An explicitly combined quarterly/annual release is one announcement,
            # not two conflicting periods. Retain both stated reporting scopes.
            periods.add(
                f"Q{quarter} and FY{fiscal[4]}"
                if fiscal[2]
                else f"Q{quarter} {'FY' if fiscal[3] else ''}{fiscal[4]}"
            )
    if len(periods) > 1:
        raise PayloadError("conflicting explicit earnings fiscal periods require review")
    period = next(iter(periods), None)
    period_ends = set()
    for statement in ANNOUNCEMENT.finditer(normalize_text(text)):
        for ending in re.finditer(rf"(?i)\bquarter\s+ended\s+{DATE_PATTERN}", statement.group(0)):
            period_ends.add(date_parser.parse(ending.group("date"), fuzzy=False).date())
    if len(period_ends) > 1:
        raise PayloadError("conflicting explicit reporting-period ends require review")
    period_end = next(iter(period_ends), None)
    if period is None and period_end is not None:
        # Preserve the disclosed period end without inferring a fiscal quarter/year.
        period = f"quarter ended {period_end.isoformat()}"
    return [
        EventCandidate(
            event_type=EventType.EARNINGS_RELEASE,
            ticker=ticker,
            exchange=exchange,
            company_name=company_name,
            cik=filing.cik,
            title=f"{company_name} earnings release",
            event_date=event_date,
            scheduled_start=start,
            source_timezone=str(start.tzinfo) if start else None,
            session=session,
            fiscal_period=period,
            status=EventStatus.CONFIRMED,
            time_precision=precision,
            source_id="sec_edgar",
            source_event_id=f"{filing.accession}:{url.rsplit('/', 1)[-1]}",
            source_url=url,
            collected_at=observed,
            parser_version=NATIVE_PARSER_VERSION,
            evidence_locator="explicit forward results sentence in SEC filing document",
            metadata={
                "accession_number": filing.accession,
                "filed_on": filing.filed_on.isoformat(),
                "form": filing.form,
                "reporting_period_end": period_end.isoformat() if period_end else None,
                "conference_call": conference_call_fact(text, event_date),
                "document_format": (
                    "local_ocr"
                    if extracted_text is not None
                    else "pdf_text_layer"
                    if is_pdf
                    else "html_or_text"
                ),
            },
        )
    ]
