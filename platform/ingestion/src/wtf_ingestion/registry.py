from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.domain.enums import CoverageState
from wtf_ingestion.domain.models import SourceRecord
from wtf_ingestion.persistence.models import (
    Company,
    Source,
    SourceEndpoint,
    UniverseMembership,
    UniverseVersion,
)

SOURCE_RECORDS = [
    SourceRecord(
        key="sec_edgar",
        name="SEC EDGAR",
        owner="U.S. Securities and Exchange Commission",
        source_type="regulatory_filing",
        base_url="https://data.sec.gov/",
        permission_status="approved_fixture_live_gated",
        access_method="JSON and filing exhibits",
        rate_policy=(
            "maximum 8 aggregate requests/second; official aggregate ceiling 10 requests/second"
        ),
        live_enabled=False,
        fixture_only=True,
        reviewed_at=date(2026, 9, 30),
        parser_version="sec-explicit-announcement-v1",
        limitations="No structured future calendar; only explicit announcement text is accepted.",
    ),
    SourceRecord(
        key="issuer_demo",
        name="Sanitized issuer IR fixture",
        owner="Fixture only",
        source_type="issuer_ir",
        base_url="https://investor.example.invalid/",
        permission_status="fixture_only",
        access_method="HTML",
        rate_policy="no live requests",
        live_enabled=False,
        fixture_only=True,
        reviewed_at=date(2026, 9, 30),
        parser_version="issuer-ir-article-v1",
        limitations="Demonstrates the adapter boundary; no issuer host is enabled.",
    ),
    SourceRecord(
        key="bls_calendar",
        name="BLS release calendar",
        owner="U.S. Bureau of Labor Statistics",
        source_type="official_ics",
        base_url="https://www.bls.gov/",
        permission_status="approved",
        access_method="ICS",
        rate_policy="one conditional request daily",
        live_enabled=False,
        fixture_only=True,
        reviewed_at=date(2026, 9, 30),
        parser_version="official-ics-v1",
        limitations="Schedule metadata only; dates can be revised.",
    ),
    SourceRecord(
        key="bea_calendar",
        name="BEA release calendar",
        owner="U.S. Bureau of Economic Analysis",
        source_type="official_ics",
        base_url="https://www.bea.gov/",
        permission_status="approved",
        access_method="ICS",
        rate_policy="one conditional request daily",
        live_enabled=False,
        fixture_only=True,
        reviewed_at=date(2026, 9, 30),
        parser_version="official-ics-v1",
        limitations="Schedule metadata only; overlapping releases remain separate by source.",
    ),
]

SAMPLE_COMPANIES = [
    ("AAPL", "NASDAQ", "Apple Inc.", "0000320193"),
    ("MSFT", "NASDAQ", "Microsoft Corporation", "0000789019"),
    ("AMZN", "NASDAQ", "Amazon.com, Inc.", "0001018724"),
    ("JPM", "NYSE", "JPMorgan Chase & Co.", "0000019617"),
    ("XOM", "NYSE", "Exxon Mobil Corporation", "0000034088"),
]

SOURCE_ENDPOINTS = {
    "sec_edgar": (
        "https://data.sec.gov/submissions/CIK{cik}.json",
        "data.sec.gov",
        "filing discovery",
        "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data",
        "https://www.sec.gov/robots.txt",
        "live_gated",
    ),
    "issuer_demo": (
        "https://investor.example.invalid/releases/{id}",
        "investor.example.invalid",
        "sanitized fixture boundary",
        None,
        None,
        "fixture_only",
    ),
    "bls_calendar": (
        "https://www.bls.gov/schedule/news_release/bls.ics",
        "www.bls.gov",
        "official release calendar",
        "https://www.bls.gov/developers/termsOfService.htm",
        "https://www.bls.gov/robots.txt",
        "approved",
    ),
    "bea_calendar": (
        "https://www.bea.gov/news/schedule/icalendar",
        "www.bea.gov",
        "official release calendar",
        "https://www.bea.gov/help/faq/145",
        "https://www.bea.gov/robots.txt",
        "approved",
    ),
}


def seed_registry(session: Session) -> None:
    for record in SOURCE_RECORDS:
        existing = session.scalar(select(Source).where(Source.key == record.key))
        values = record.model_dump(mode="python")
        values["base_url"] = str(record.base_url)
        values["next_review_at"] = record.reviewed_at + timedelta(days=90)
        if existing:
            for key, value in values.items():
                protected = {"live_enabled", "fixture_only", "permission_status"}
                if existing.live_enabled:
                    protected.add("parser_version")
                if existing.key == "sec_edgar" and not existing.fixture_only:
                    # Reseeding test definitions cannot reset a real source's
                    # effective parser/rate, including after an access block.
                    protected.update({"parser_version", "rate_policy"})
                if key not in protected:
                    setattr(existing, key, value)
        else:
            existing = Source(**values)
            session.add(existing)
            session.flush()
        endpoint_values = SOURCE_ENDPOINTS[record.key]
        endpoint = session.scalar(
            select(SourceEndpoint).where(
                SourceEndpoint.source_id == existing.id,
                SourceEndpoint.url == endpoint_values[0],
            )
        )
        if endpoint is None:
            session.add(
                SourceEndpoint(
                    source_id=existing.id,
                    url=endpoint_values[0],
                    host=endpoint_values[1],
                    purpose=endpoint_values[2],
                    terms_url=endpoint_values[3],
                    robots_url=endpoint_values[4],
                    approval_state=endpoint_values[5],
                )
            )

    universe = session.scalar(select(UniverseVersion).where(UniverseVersion.version == "sample-v1"))
    sec = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    archive_url = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
    if sec and not session.scalar(
        select(SourceEndpoint).where(
            SourceEndpoint.source_id == sec.id, SourceEndpoint.url == archive_url
        )
    ):
        session.add(
            SourceEndpoint(
                source_id=sec.id,
                url=archive_url,
                host="www.sec.gov",
                purpose="SEC filing documents and explicit earnings evidence",
                terms_url="https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data",
                robots_url="https://www.sec.gov/robots.txt",
                approval_state="approved" if sec.live_enabled else "live_gated",
            )
        )
    if universe is None:
        universe = UniverseVersion(
            name="Configurable development sample",
            version="sample-v1",
            effective_from=date(2026, 9, 30),
            assumption=True,
            source_url=None,
        )
        session.add(universe)
        session.flush()
    for ticker, exchange, name, cik in SAMPLE_COMPANIES:
        company = session.scalar(
            select(Company).where(Company.ticker == ticker, Company.exchange == exchange)
        )
        if company is None:
            company = Company(
                ticker=ticker, exchange=exchange, company_name=name, cik=cik, aliases={}
            )
            session.add(company)
            session.flush()
        membership = session.scalar(
            select(UniverseMembership).where(
                UniverseMembership.universe_version_id == universe.id,
                UniverseMembership.company_id == company.id,
            )
        )
        if membership is None:
            session.add(
                UniverseMembership(
                    universe_version_id=universe.id,
                    company_id=company.id,
                    coverage_state=CoverageState.NOT_YET_ANNOUNCED.value,
                    state_reason="No accepted announcement in the current 90-day fixture window.",
                )
            )
