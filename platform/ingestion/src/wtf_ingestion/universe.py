from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.persistence.locking import pipeline_lock
from wtf_ingestion.persistence.models import (
    Company,
    EventCurrent,
    UniverseMembership,
    UniverseVersion,
)
from wtf_ingestion.safety import validate_public_url


class UniverseCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9.-]+$")
    exchange: str = Field(min_length=1, max_length=64)
    company_name: str = Field(min_length=1, max_length=300)
    cik: str = Field(pattern=r"^\d{10}$")


class ListingReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    exchange: str
    previous_cik: str = Field(pattern=r"^\d{10}$")
    current_cik: str = Field(pattern=r"^\d{10}$")
    evidence_url: str
    reviewed_at: date


class UniverseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)
    effective_from: date
    assumption: bool = True
    provenance: dict[str, Any] = Field(default_factory=dict)
    companies: list[UniverseCompany] = Field(min_length=1, max_length=1000)
    reconciliations: list[ListingReconciliation] = Field(default_factory=list)


def import_universe(session: Session, path: Path) -> str:
    data = UniverseInput.model_validate_json(path.read_bytes())
    for reconciliation in data.reconciliations:
        validate_public_url(reconciliation.evidence_url)
        parsed = urlsplit(reconciliation.evidence_url)
        if parsed.hostname != "www.sec.gov" or not parsed.path.startswith(
            f"/Archives/edgar/data/{int(reconciliation.current_cik)}/"
        ):
            raise ValueError("issuer reconciliation requires reviewed SEC successor evidence")
    identities = [(item.ticker, item.exchange) for item in data.companies]
    if len(set(identities)) != len(identities):
        raise ValueError("universe contains duplicate ticker/exchange or CIK identities")
    digest = hashlib.sha256(
        json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    with pipeline_lock(session):
        existing = session.scalar(
            select(UniverseVersion).where(UniverseVersion.version == data.version)
        )
        if existing:
            if existing.source_url != f"sha256:{digest}":
                raise ValueError(
                    "an existing universe version cannot be overwritten; issue a new version"
                )
            return existing.version
        universe = UniverseVersion(
            name=data.name,
            version=data.version,
            effective_from=data.effective_from,
            assumption=data.assumption,
            source_url=f"sha256:{digest}",
            provenance={
                **data.provenance,
                **(
                    {
                        "reconciliations": [
                            row.model_dump(mode="json") for row in data.reconciliations
                        ]
                    }
                    if data.reconciliations
                    else {}
                ),
            },
        )
        session.add(universe)
        session.flush()
        grouped: dict[str, list[UniverseCompany]] = defaultdict(list)
        for item in data.companies:
            grouped[item.cik].append(item)
        for _cik, items in sorted(grouped.items()):
            item = sorted(items, key=lambda row: (row.ticker, row.exchange))[0]
            company = session.scalar(select(Company).where(Company.cik == item.cik))
            if company and (company.ticker, company.exchange) not in {
                (row.ticker, row.exchange) for row in items
            }:
                raise ValueError("changed issuer mapping requires explicit reconciliation")
            if company is None:
                for listing in items:
                    predecessors = session.scalars(
                        select(Company).where(
                            Company.ticker == listing.ticker,
                            Company.exchange == listing.exchange,
                            Company.cik != listing.cik,
                        )
                    ).all()
                    for predecessor in predecessors:
                        if not any(
                            row.ticker == listing.ticker
                            and row.exchange == listing.exchange
                            and row.previous_cik == predecessor.cik
                            and row.current_cik == listing.cik
                            for row in data.reconciliations
                        ):
                            raise ValueError(
                                "changed issuer mapping requires explicit reconciliation"
                            )
                company = Company(**item.model_dump())
                session.add(company)
                session.flush()
            session.add(
                UniverseMembership(
                    universe_version_id=universe.id,
                    company_id=company.id,
                    coverage_state="not_yet_announced",
                    state_reason="No date inferred.",
                    listings=[{"ticker": row.ticker, "exchange": row.exchange} for row in items],
                )
            )
        session.commit()
    return data.version


def active_universe(session: Session) -> UniverseVersion | None:
    today = datetime.now(UTC).date()
    return session.scalar(
        select(UniverseVersion)
        .where(
            UniverseVersion.effective_from <= today,
            UniverseVersion.effective_to.is_(None) | (UniverseVersion.effective_to >= today),
        )
        .order_by(UniverseVersion.effective_from.desc(), UniverseVersion.created_at.desc())
        .limit(1)
    )


def in_fixed_universe(session: Session, events: Sequence[EventCurrent]) -> list[EventCurrent]:
    universe = active_universe(session)
    if universe is None:
        return list(events)
    ciks = set(
        session.scalars(
            select(Company.cik)
            .join(UniverseMembership)
            .where(UniverseMembership.universe_version_id == universe.id)
        )
    )
    return [
        event for event in events if event.event_type != "earnings_release" or event.cik in ciks
    ]
