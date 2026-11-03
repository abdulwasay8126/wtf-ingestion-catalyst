from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.adapters.sec_native import Filing, parse_submissions
from wtf_ingestion.domain.enums import EventStatus, EventType, Session, TimePrecision
from wtf_ingestion.domain.models import EventCandidate

METHOD = "sec_item_2_02_52_week_v1"


@dataclass(frozen=True)
class Projection:
    candidates: list[EventCandidate]
    reason: str


def project_next_earnings(
    raw: bytes,
    *,
    cik: str,
    ticker: str,
    exchange: str,
    company_name: str,
    observed: datetime,
    horizon_days: int = 90,
) -> Projection:
    """A disclosed filing-cadence proxy, never an official earnings-date assertion."""
    if observed.tzinfo is None:
        raise PayloadError("projection observation requires an explicit timezone")
    observed = observed.astimezone(UTC)
    filings = parse_submissions(raw, cik, observed, lookback_days=364)
    unique: dict[str, Filing] = {}
    for filing in filings:
        if filing.form != "8-K" or "2.02" not in re.findall(r"\d+\.\d{2}", filing.items):
            continue
        estimated = filing.filed_on + timedelta(weeks=52)
        if not observed.date() <= estimated <= observed.date() + timedelta(days=horizon_days):
            continue
        previous = unique.get(filing.accession)
        if previous and previous != filing:
            raise PayloadError("conflicting SEC estimate basis accession")
        unique[filing.accession] = filing
    bases = sorted(unique.values(), key=lambda item: (item.filed_on, item.accession))
    if not bases:
        return Projection([], "no_prior_year_item_2_02_basis")
    if len(bases) > 1 and (bases[1].filed_on - bases[0].filed_on).days <= 7:
        return Projection([], "ambiguous_nearby_item_2_02_bases")
    basis = bases[0]
    # A joint filing's document can live under a co-registrant's CIK. The filing
    # index identifies the supplied accession without inventing its document directory.
    url = basis.index_url
    return Projection(
        [
            EventCandidate(
                event_type=EventType.EARNINGS_RELEASE,
                ticker=ticker,
                exchange=exchange,
                company_name=company_name,
                cik=cik,
                title=f"{company_name} earnings timing estimate (SEC filing cadence)",
                event_date=basis.filed_on + timedelta(weeks=52),
                scheduled_start=None,
                session=Session.UNKNOWN,
                fiscal_period=None,
                status=EventStatus.ESTIMATED,
                time_precision=TimePrecision.DATE_ONLY,
                source_id="sec_edgar",
                source_event_id=f"projection:{basis.accession}",
                source_url=url,
                collected_at=observed,
                parser_version=METHOD,
                evidence_locator=(
                    f"SEC submissions Item 2.02 filing {basis.accession}; 52-week projection"
                ),
                metadata={
                    "estimate_method": METHOD,
                    "estimate_basis_date": basis.filed_on.isoformat(),
                    "estimate_basis_url": url,
                    "estimate_basis_accession": basis.accession,
                    "estimate_basis_primary_document": basis.primary_document,
                    "estimate_basis_url_kind": "filing_index",
                    "estimate_basis_items": basis.items,
                    "estimate_basis_raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "estimate_confidence": "low",
                    "basis_is_filing_date_proxy": True,
                    "projection_horizon_days": horizon_days,
                },
            )
        ],
        "estimated_from_filing_date_proxy",
    )
