from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from wtf_ingestion.adapters.base import FetchPolicy, PayloadError, safe_fetch
from wtf_ingestion.adapters.parsing import extract_explicit_announcement
from wtf_ingestion.config import Settings
from wtf_ingestion.domain.enums import EventStatus, EventType
from wtf_ingestion.domain.models import EventCandidate


class SecEarningsAdapter:
    source_id = "sec_edgar"
    parser_version = "sec-explicit-announcement-v1"
    submissions_url = "https://data.sec.gov/submissions/CIK{cik}.json"

    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch_submissions(self, cik: str) -> bytes:
        self.settings.assert_live_sec_ready()
        url = self.submissions_url.format(cik=cik.zfill(10))
        body, _headers = safe_fetch(
            url,
            FetchPolicy(
                allowed_hosts=frozenset({"data.sec.gov"}),
                user_agent=self.settings.sec_user_agent or "",
                timeout_seconds=self.settings.request_timeout_seconds,
                max_response_bytes=self.settings.max_response_bytes,
                content_types=("application/json",),
            ),
        )
        return body

    def parse_fixture(
        self, raw: bytes, collected_at: datetime | None = None
    ) -> list[EventCandidate]:
        observed = collected_at or datetime.now(UTC)
        try:
            payload: dict[str, Any] = json.loads(raw)
            filings = payload["filings"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PayloadError("malformed SEC fixture boundary") from exc
        if not isinstance(filings, list):
            raise PayloadError("SEC filings must be a list")
        candidates: list[EventCandidate] = []
        for filing in filings:
            if filing.get("form") != "8-K" or "2.02" not in filing.get("items", []):
                continue
            text = str(filing.get("exhibit_text", ""))
            try:
                event_date, local_start, session, precision = extract_explicit_announcement(text)
            except ValueError as exc:
                if "ambiguous" in str(exc):
                    raise PayloadError("multiple earnings dates require review") from exc
                continue
            if event_date < observed.date():
                continue
            source_url = str(filing["exhibit_url"])
            candidates.append(
                EventCandidate(
                    event_type=EventType.EARNINGS_RELEASE,
                    ticker=filing.get("ticker"),
                    exchange=filing.get("exchange"),
                    company_name=filing.get("company_name"),
                    cik=filing.get("cik"),
                    title=filing.get("title") or "Earnings release",
                    event_date=event_date,
                    scheduled_start=local_start,
                    source_timezone=str(local_start.tzinfo) if local_start else None,
                    session=session,
                    fiscal_period=filing.get("fiscal_period"),
                    status=EventStatus.CONFIRMED,
                    time_precision=precision,
                    source_id=self.source_id,
                    source_event_id=f"{filing['accession_number']}:{filing['exhibit_sequence']}",
                    source_url=source_url,
                    source_published_at=datetime.fromisoformat(filing["filed_at"]),
                    collected_at=observed,
                    parser_version=self.parser_version,
                    evidence_locator="explicit future-date sentence in filed exhibit",
                    metadata={"accession_number": filing["accession_number"], "item": "2.02"},
                )
            )
        return candidates
