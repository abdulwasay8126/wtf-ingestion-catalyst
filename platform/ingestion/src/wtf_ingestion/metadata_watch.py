"""Check public submissions for changes without renewing accepted event evidence.

The caller holds the exclusive SEC worker lease. A changed body requests the
existing complete collector; this module cannot accept dates or publish a feed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.base import (
    AccessBlockedError,
    FetchPolicy,
    PayloadError,
    RequestGate,
    SourcePermissionError,
    ThrottledError,
)
from wtf_ingestion.adapters.prefetch import CacheReference, MetadataPrefetch, Result
from wtf_ingestion.adapters.sec_native import ACCESSION, DOCUMENT, parse_submissions
from wtf_ingestion.config import Settings
from wtf_ingestion.persistence.locking import pipeline_lock
from wtf_ingestion.persistence.models import (
    Company,
    EndpointCache,
    SecMetadataCheck,
    Source,
    UniverseMembership,
)
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.universe import active_universe

WATCH_VERSION = "sec-metadata-watch-v1"


def validate_metadata(raw: bytes, cik: str, observed: datetime) -> None:
    # A metadata check is not a historical-window completeness claim. A zero-day
    # scan still validates every parallel row, issuer identity and future dates.
    parse_submissions(raw, cik, observed, lookback_days=0)
    try:
        payload: dict[str, Any] = json.loads(raw)
        recent = payload["filings"]["recent"]
        if not recent["accessionNumber"]:
            raise PayloadError("Empty submissions cannot renew a configured issuer check")
        for field in ("accessionNumber", "form", "items", "primaryDocument"):
            if any(not isinstance(value, str) for value in recent[field]):
                raise PayloadError("SEC metadata rows contain invalid field types")
        for index, form in enumerate(recent["form"]):
            if form in {"8-K", "8-K/A"} and (
                not ACCESSION.fullmatch(recent["accessionNumber"][index])
                or not DOCUMENT.fullmatch(recent["primaryDocument"][index])
            ):
                raise PayloadError("SEC metadata filing identifiers are malformed")
        files = payload["filings"].get("files", [])
        if not isinstance(files, list):
            raise PayloadError("SEC declared archive metadata is malformed")
    except (KeyError, TypeError, ValueError) as exc:
        raise PayloadError("SEC metadata structure requires review") from exc


def check_sec_metadata(session: Session, cfg: Settings) -> SecMetadataCheck:
    """One bounded all-issuer check, called only while holding WORKER_LOCK."""
    # Also exclude direct collectors, publication and separate OCR capture. The
    # worker lease alone does not exclude those operator commands.
    with pipeline_lock(session):
        return _check_sec_metadata(session, cfg)


def _check_sec_metadata(session: Session, cfg: Settings) -> SecMetadataCheck:
    cfg.assert_live_sec_ready()
    source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    universe = active_universe(session)
    if (
        source is None
        or not source.live_enabled
        or source.fixture_only
        or source.permission_status != "approved"
        or universe is None
    ):
        raise SourcePermissionError("Reviewed SEC access and fixed universe are required")
    companies = session.scalars(
        select(Company)
        .join(UniverseMembership)
        .where(UniverseMembership.universe_version_id == universe.id)
        .order_by(Company.cik)
    ).all()
    if not companies or any(company.cik is None for company in companies):
        raise PayloadError("Metadata watch requires the complete reviewed CIK universe")
    urls = {
        f"https://data.sec.gov/submissions/CIK{company.cik}.json": company.cik
        for company in companies
    }
    caches = {
        cache.url: cache
        for cache in session.scalars(
            select(EndpointCache).where(EndpointCache.source_id == source.id)
        )
        if cache.url in urls
    }
    references = {
        url: CacheReference(Path(cache.storage_path), cache.payload_sha256, cache.safe_headers)
        for url, cache in caches.items()
    }
    policy = FetchPolicy(
        frozenset({"data.sec.gov"}),
        cfg.sec_user_agent or "",
        timeout_seconds=cfg.request_timeout_seconds,
        max_response_bytes=cfg.max_response_bytes,
        content_types=("application/json",),
        min_interval_seconds=1 / cfg.sec_requests_per_second,
        stop_on_throttle=True,
    )
    gate = RequestGate(1 / cfg.sec_requests_per_second)
    started = datetime.now(UTC)
    observations: list[dict[str, Any]] = []
    changed: list[str] = []
    ledger: list[dict[str, Any]] = []
    error_code: str | None = None
    prefetch = MetadataPrefetch([], policy, gate, cfg.sec_metadata_workers)

    def audit(url: str, result: Result) -> None:
        if isinstance(result.error, (AccessBlockedError, ThrottledError)):
            source.live_enabled = False
            source.permission_status = "access_review_required"
        ledger.append(
            {
                "cik": urls[url],
                "observed_at": result.observed.isoformat(),
                "attempts": result.attempts,
            }
        )

    try:
        prefetch.prime(list(urls), references)
        for url, cik in urls.items():
            result = prefetch.take(url)
            audit(url, result)
            if result.error:
                raise result.error
            assert cik is not None
            validate_metadata(result.body, cik, result.observed)
            digest = hashlib.sha256(result.body).hexdigest()
            cache = caches.get(url)
            differs = cache is None or digest != cache.payload_sha256
            if differs:
                changed.append(cik)
                # Content-addressed capture, not an endpoint-cache validation or
                # accepted assertion. The collector must fetch and validate anew.
                IngestionService(session, cfg.raw_dir)._store_raw(digest, result.body)
            observations.append(
                {
                    "cik": cik,
                    "sha256": digest,
                    "observed_at": result.observed.isoformat(),
                    "changed": differs,
                    "baseline_sha256": cache.payload_sha256 if cache else None,
                }
            )
    except Exception as exc:
        error_code = getattr(exc, "code", "METADATA_WATCH_ERROR")
        if isinstance(exc, (AccessBlockedError, ThrottledError)):
            source.live_enabled = False
            source.permission_status = "access_review_required"
    finally:
        for url, result in prefetch.close():
            audit(url, result)
    check = SecMetadataCheck(
        source_id=source.id,
        universe_version_id=universe.id,
        started_at=started,
        finished_at=datetime.now(UTC),
        state="failed" if error_code else "changed" if changed else "unchanged",
        payload={
            "version": WATCH_VERSION,
            "checked": len(observations),
            "total": len(urls),
            "changed_ciks": changed,
            "error_code": error_code,
            "observations": observations,
            "http_ledger": ledger,
            "accepted_evidence_renewed": False,
        },
    )
    session.add(check)
    session.commit()
    return check
