from __future__ import annotations

from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.adapters.parsing import extract_explicit_announcement
from wtf_ingestion.domain.enums import EventStatus, EventType
from wtf_ingestion.domain.models import EventCandidate


class IssuerIrHtmlAdapter:
    parser_version = "issuer-ir-article-v1"

    def __init__(
        self,
        *,
        source_id: str,
        ticker: str,
        exchange: str,
        company_name: str,
        cik: str,
        source_url: str,
        permission_approved: bool = False,
    ):
        self.source_id = source_id
        self.ticker = ticker
        self.exchange = exchange
        self.company_name = company_name
        self.cik = cik
        self.source_url = source_url
        self.permission_approved = permission_approved

    def parse_fixture(
        self, raw: bytes, collected_at: datetime | None = None
    ) -> list[EventCandidate]:
        observed = collected_at or datetime.now(UTC)
        tree = HTMLParser(raw)
        article = tree.css_first("article[data-event-id]")
        if article is None:
            raise PayloadError("expected issuer announcement article was not found")
        event_id = article.attributes.get("data-event-id")
        title_node = article.css_first("h1")
        body_node = article.css_first(".release-body")
        if not event_id or title_node is None or body_node is None:
            raise PayloadError("issuer announcement is incomplete")
        body = body_node.text(separator=" ", strip=True)
        try:
            event_date, local_start, session, precision = extract_explicit_announcement(body)
        except ValueError as exc:
            if "ambiguous" in str(exc):
                raise PayloadError("multiple explicit earnings dates require review") from exc
            return []
        if event_date < observed.date():
            return []
        return [
            EventCandidate(
                event_type=EventType.EARNINGS_RELEASE,
                ticker=self.ticker,
                exchange=self.exchange,
                company_name=self.company_name,
                cik=self.cik,
                title=title_node.text(strip=True),
                event_date=event_date,
                scheduled_start=local_start,
                source_timezone=str(local_start.tzinfo) if local_start else None,
                session=session,
                fiscal_period=article.attributes.get("data-fiscal-period"),
                status=EventStatus.CONFIRMED,
                time_precision=precision,
                source_id=self.source_id,
                source_event_id=event_id,
                source_url=self.source_url,
                collected_at=observed,
                parser_version=self.parser_version,
                evidence_locator="official IR release body",
            )
        ]
