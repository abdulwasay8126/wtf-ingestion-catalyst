from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.base import (
    FetchPolicy,
    IncompleteScanError,
    PayloadError,
    PayloadTooLargeError,
    RequestGate,
    SafeHttpClient,
    SourcePermissionError,
    UnreadableDocumentError,
)
from wtf_ingestion.adapters.ocr import (
    OCR_VERSION,
    OcrQualityError,
    engine_context,
    recognize_image,
    render_pdf,
)
from wtf_ingestion.adapters.prefetch import CacheReference, MetadataPrefetch, Result
from wtf_ingestion.adapters.sec_estimates import project_next_earnings
from wtf_ingestion.adapters.sec_history import complete_submissions
from wtf_ingestion.adapters.sec_native import (
    NATIVE_PARSER_VERSION,
    Filing,
    parse_document_index,
    parse_exhibit,
    parse_submissions,
)
from wtf_ingestion.config import Settings
from wtf_ingestion.domain.models import EventCandidate
from wtf_ingestion.health import as_utc
from wtf_ingestion.ocr_trial import indexed_images
from wtf_ingestion.persistence.models import (
    CollectionRun,
    Company,
    EndpointCache,
    HttpAttempt,
    RawObject,
    SecCompanyCheckpoint,
    SecDocumentParse,
    Source,
    UniverseMembership,
    UniverseVersion,
)
from wtf_ingestion.pipeline import IngestionService, IngestResult
from wtf_ingestion.safety import validate_public_url

RESUME_WINDOW = timedelta(hours=6)
ARCHIVE_RECHECK = timedelta(hours=24)


class SecCollector:
    """Actual SEC formats; bounded fixed-universe scan, not a forward calendar API."""

    def __init__(
        self,
        settings: Settings,
        session: Session,
        client: SafeHttpClient | None = None,
        progress: Callable[[UUID, int, int, str], None] | None = None,
    ):
        self.settings = settings
        self.session = session
        self.service = IngestionService(
            session,
            settings.raw_dir,
            output_dir=settings.output_dir,
            allow_estimates=settings.enable_sec_estimates,
        )
        self.client = client
        self.progress = progress
        self.progress_state = (0, 0, "")
        self.network_requests = 0
        self.reused_objects = 0
        self.captured: dict[str, tuple[str, httpx.Headers, datetime]] = {}
        self.current_url: str | None = None
        self.reused_parses = 0
        self.evidence_mode = "fixture" if client is not None else "live"
        self.run_id: UUID | None = None
        self.review_urls: set[str] = set()
        self.endpoint_caches: dict[str, EndpointCache | None] = {}
        self.parse_caches: dict[str, SecDocumentParse] = {}
        self.verified_bodies: OrderedDict[str, bytes] = OrderedDict()
        self.verified_body_bytes = 0
        self.prefetch: MetadataPrefetch | None = None
        self.archive_prefetch: MetadataPrefetch | None = None
        self.ocr_evidence: dict[str, str] = {}
        self.pdf_ocr_proofs: dict[tuple[str, str], str] = {}

    def run(self, trigger: str = "manual", evidence_mode: str = "live") -> IngestResult:
        if evidence_mode == "live":
            self.settings.assert_live_sec_ready()
        elif self.client is None:
            raise RuntimeError("fixture collection requires an injected offline transport")
        self.evidence_mode = evidence_mode
        return self.service.collect(
            source_key="sec_edgar",
            fetch=self._fetch,
            parse=self._parse,
            trigger=trigger,
            evidence_mode=evidence_mode,
            allow_empty=True,
        )

    def _fetch(self, run_id: UUID) -> tuple[bytes, str, str]:
        self.run_id = run_id
        today = datetime.now(UTC).date()
        universe = self.session.scalar(
            select(UniverseVersion)
            .where(
                UniverseVersion.effective_from <= today,
                UniverseVersion.effective_to.is_(None) | (UniverseVersion.effective_to >= today),
            )
            .order_by(UniverseVersion.effective_from.desc(), UniverseVersion.created_at.desc())
            .limit(1)
        )
        if universe is None:
            raise PayloadError("a current fixed universe is required")
        scope_run = self.session.get(CollectionRun, run_id)
        if scope_run is None:
            raise RuntimeError("collection run disappeared")
        scope_run.universe_version_id = universe.id
        ranked = (
            select(
                SecCompanyCheckpoint.id,
                func.row_number()
                .over(
                    partition_by=SecCompanyCheckpoint.company_id,
                    order_by=(SecCompanyCheckpoint.saved_at.desc(), SecCompanyCheckpoint.id.desc()),
                )
                .label("position"),
            )
            .join(CollectionRun, CollectionRun.id == SecCompanyCheckpoint.run_id)
            .where(
                CollectionRun.source_id == scope_run.source_id,
                CollectionRun.evidence_mode == scope_run.evidence_mode,
                SecCompanyCheckpoint.universe_version_id == universe.id,
            )
            .subquery()
        )
        previous_checks = self.session.scalars(
            select(SecCompanyCheckpoint)
            .join(ranked, SecCompanyCheckpoint.id == ranked.c.id)
            .where(ranked.c.position == 1)
        ).all()
        latest_checks: dict[UUID, SecCompanyCheckpoint] = {}
        for checkpoint in previous_checks:
            latest_checks.setdefault(checkpoint.company_id, checkpoint)
        self.review_urls = {
            row.payload["issue_url"]
            for row in latest_checks.values()
            if row.state == "REVIEW_REQUIRED" and row.payload.get("issue_url")
        }
        self.pdf_ocr_proofs = {
            (document["url"], document["body_sha256"]): document["ocr_sha256"]
            for row in latest_checks.values()
            if row.state == "VALIDATED"
            for document in row.payload.get("documents", [])
            if document["url"].lower().endswith(".pdf") and document.get("ocr_sha256")
        }
        source = self.session.get(Source, scope_run.source_id)
        if source:
            source.parser_version = NATIVE_PARSER_VERSION
            source.rate_policy = (
                f"maximum {self.settings.sec_requests_per_second:g} aggregate requests/second; "
                "official aggregate ceiling 10 requests/second; stop on 403/429"
            )
        self.session.commit()
        # Keep metadata, not the full universe's documents, in memory. Per-document
        # SELECT/COMMIT round trips dominated warm scans on the Windows Docker mount.
        self.endpoint_caches = {
            row.url: row
            for row in self.session.scalars(
                select(EndpointCache)
                .where(EndpointCache.source_id == scope_run.source_id)
                .order_by(EndpointCache.validated_at.desc())
                .limit(30000)
            )
        }
        self.parse_caches = {
            row.key: row
            for row in self.session.scalars(
                select(SecDocumentParse)
                .where(
                    SecDocumentParse.source_id == scope_run.source_id,
                    SecDocumentParse.evidence_mode == self.evidence_mode,
                    SecDocumentParse.parser_version == NATIVE_PARSER_VERSION,
                )
                .order_by(SecDocumentParse.parsed_for_date.desc())
                .limit(30000)
            )
        }
        companies = self.session.scalars(
            select(Company)
            .join(UniverseMembership)
            .where(UniverseMembership.universe_version_id == universe.id)
            .order_by(Company.cik)
        ).all()
        if not companies or any(company.cik is None for company in companies):
            raise PayloadError("all fixed-universe companies need verified CIKs")
        client = self.client or SafeHttpClient(
            FetchPolicy(
                frozenset({"data.sec.gov", "www.sec.gov"}),
                self.settings.sec_user_agent or "",
                timeout_seconds=self.settings.request_timeout_seconds,
                max_response_bytes=self.settings.max_response_bytes,
                min_interval_seconds=1.0 / self.settings.sec_requests_per_second,
                stop_on_throttle=True,
            )
        )
        if self.client is None and self.settings.sec_metadata_workers > 1:
            gate = RequestGate(1.0 / self.settings.sec_requests_per_second)
            client.gate = gate
            self.prefetch = MetadataPrefetch(
                [
                    f"https://data.sec.gov/submissions/CIK{company.cik}.json"
                    for company in companies
                ],
                client.policy,
                gate,
                self.settings.sec_metadata_workers,
            )
            self.archive_prefetch = MetadataPrefetch(
                [], client.policy, gate, self.settings.sec_metadata_workers
            )
        config_key = hashlib.sha256(
            json.dumps(
                {
                    "date": today.isoformat(),
                    "parser": NATIVE_PARSER_VERSION,
                    "estimates": self.settings.enable_sec_estimates,
                    "horizon": self.settings.publication_horizon_days,
                    "ocr": self.settings.enable_sec_ocr,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        company_payloads = []
        issues = []
        try:
            for position, company in enumerate(companies):
                self.progress_state = (position, len(companies), company.ticker)
                if self.progress:
                    self.progress(run_id, *self.progress_state)
                self.captured = {}
                self.ocr_evidence = {}
                self.network_requests = self.reused_objects = self.reused_parses = 0
                self.current_url = None
                checked_at = datetime.now(UTC)
                try:
                    # Always check today's metadata; comparison replaces blind company
                    # replay so newly added, revised or removed filings are not missed.
                    data, checked_at, candidates = self._fetch_company(client, run_id, company)
                    unique: dict[str, EventCandidate] = {}
                    for candidate in candidates:
                        if candidate.status.value == "estimated":
                            self.service._check_estimate(candidate)
                        prior = unique.get(candidate.canonical_key)
                        if prior and prior.content_hash != candidate.content_hash:
                            raise PayloadError("conflicting source facts in company checkpoint")
                        unique[candidate.canonical_key] = candidate
                    data = {
                        **data,
                        "candidates": [item.model_dump(mode="json") for item in unique.values()],
                    }
                    checkpoint = SecCompanyCheckpoint(
                        run_id=run_id,
                        universe_version_id=universe.id,
                        company_id=company.id,
                        parser_version=NATIVE_PARSER_VERSION,
                        config_key=config_key,
                        state="VALIDATED",
                        checked_at=checked_at,
                        saved_at=datetime.now(UTC),
                        payload=data,
                        network_requests=self.network_requests,
                        reused_objects=self.reused_objects,
                        reused_parses=self.reused_parses,
                    )
                    company_payloads.append(data)
                except PayloadError as exc:
                    # Isolate structural failures, never transport/access blocks. No facts
                    # from this company enter the preview or the official accepted feed.
                    self.session.rollback()
                    if self.archive_prefetch:
                        self._audit_unused(run_id, self.archive_prefetch.drain())
                    # Rollback can expire ORM caches and discard pending inserts.
                    # Reload on demand; never reuse a rolled-back derived result.
                    self.endpoint_caches = {}
                    self.parse_caches = {}
                    summary = (
                        "Document has no reliable complete text; source evidence needs review."
                        if exc.code == "DOCUMENT_TEXT_UNREADABLE"
                        else "OCR could not validate every page or the extracted facts disagree. "
                        "Retained OCR evidence needs review; no uncertain facts were accepted."
                        if exc.code == "OCR_QUALITY_REVIEW_REQUIRED"
                        else "Response exceeded the configured decoded-byte limit; "
                        "complete body was not retained. Review size bounds."
                        if exc.code == "PAYLOAD_TOO_LARGE"
                        else "Company evidence failed structural validation; "
                        "inspect retained raw input."
                    )
                    checkpoint = SecCompanyCheckpoint(
                        run_id=run_id,
                        universe_version_id=universe.id,
                        company_id=company.id,
                        parser_version=NATIVE_PARSER_VERSION,
                        config_key=config_key,
                        state="REVIEW_REQUIRED",
                        checked_at=checked_at,
                        saved_at=datetime.now(UTC),
                        payload={
                            "issue_url": self.current_url,
                            "candidates": [],
                            "ocr_evidence": self.ocr_evidence.copy(),
                        },
                        error_code=exc.code,
                        safe_summary=summary,
                        network_requests=self.network_requests,
                        reused_objects=self.reused_objects,
                        reused_parses=self.reused_parses,
                    )
                    issues.append(f"company_review:{company.ticker}:{exc.code}")
                self.session.add(checkpoint)
                scope_run.received_count = sum(len(item["candidates"]) for item in company_payloads)
                scope_run.anomaly_flags = issues.copy()
                self.session.commit()
                self.progress_state = (position + 1, len(companies), company.ticker)
                if self.progress:
                    self.progress(run_id, *self.progress_state)
            if issues:
                raise IncompleteScanError(
                    "Company reviews remain; official batch publication withheld."
                )
            payload = {
                "bundle_format": "raw_sha256_refs_v1",
                "universe_version": universe.version,
                "companies_scanned": len(companies),
                "complete": True,
                "documents": [
                    document for item in company_payloads for document in item["documents"]
                ],
                "projection_inputs": [
                    entry for item in company_payloads for entry in item["projection_inputs"]
                ],
                "projection_horizon_days": self.settings.publication_horizon_days,
                "estimates_enabled": self.settings.enable_sec_estimates,
            }
            run = self.session.get(CollectionRun, run_id)
            if run is None:
                raise RuntimeError("collection run disappeared")
            run.anomaly_flags = [
                f"estimate_unavailable:{item['ticker']}:{item['projection_status']}"
                for company_data in company_payloads
                for item in company_data["projection_inputs"]
                if item["projection_status"] != "estimated_from_filing_date_proxy"
            ]
            self.session.commit()
            return (
                json.dumps(payload, sort_keys=True).encode(),
                "https://data.sec.gov/submissions/",
                "application/json",
            )
        finally:
            blocked_error = None
            for prefetcher in (self.prefetch, self.archive_prefetch):
                if prefetcher is None:
                    continue
                for url, unused in prefetcher.close():
                    if unused.error and any(status == "4xx" for _, status in unused.attempts):
                        blocked_error = unused.error
                    for attempt, status in unused.attempts:
                        self.session.add(
                            HttpAttempt(
                                run_id=run_id,
                                endpoint_url=url,
                                attempt_number=attempt,
                                status_class=status,
                                observed_at=unused.observed,
                            )
                        )
                self.session.commit()
            self.prefetch = self.archive_prefetch = None
            if self.client is None:
                client.close()
            if blocked_error is not None:
                raise blocked_error

    def _fetch_company(
        self, client: SafeHttpClient, run_id: UUID, company: Company
    ) -> tuple[dict[str, Any], datetime, list[EventCandidate]]:
        if company.cik is None:
            raise PayloadError("company CIK is required")
        url = f"https://data.sec.gov/submissions/CIK{company.cik}.json"
        raw, _ = self._get(client, url, run_id, ("application/json",), refresh_metadata=True)
        checked_at = self.captured[url][2]
        archive_urls = []

        def archive_fetch(archive_url: str) -> bytes:
            archive_urls.append(archive_url)
            return self._get(client, archive_url, run_id, ("application/json",))[0]

        original = raw
        raw = complete_submissions(
            raw,
            company.cik,
            datetime.now(UTC),
            archive_fetch,
            lookback_days=364 if self.settings.enable_sec_estimates else 120,
        )
        filings = parse_submissions(raw, company.cik, datetime.now(UTC))
        for endpoint in [url, *archive_urls]:
            self._remember_validated(endpoint, run_id)
        if raw != original:
            digest = hashlib.sha256(raw).hexdigest()
            path = self.service._store_raw(digest, raw)
            self.session.add(
                RawObject(
                    run_id=run_id,
                    sha256=digest,
                    byte_size=len(raw),
                    content_type="application/json",
                    source_url=url,
                    storage_path=str(path.resolve()),
                    retrieved_at=checked_at,
                    parser_version="sec-history-merge-v1",
                )
            )
            self.session.commit()
        inputs = []
        candidates = []
        if self.settings.enable_sec_estimates:
            projection = project_next_earnings(
                raw,
                cik=company.cik,
                ticker=company.ticker,
                exchange=company.exchange,
                company_name=company.company_name,
                observed=datetime.now(UTC),
                horizon_days=self.settings.publication_horizon_days,
            )
            inputs.append(
                {
                    "cik": company.cik,
                    "ticker": company.ticker,
                    "exchange": company.exchange,
                    "company_name": company.company_name,
                    "source_url": url,
                    "body_sha256": hashlib.sha256(raw).hexdigest(),
                    "retrieved_at": checked_at.isoformat(),
                    "projection_status": projection.reason,
                }
            )
            for candidate in projection.candidates:
                candidate.collected_at = checked_at
                candidate.raw_payload_hash = hashlib.sha256(raw).hexdigest()
            candidates.extend(projection.candidates)
        documents = []
        plans = []
        self._prime_archives(run_id, [filing.index_url for filing in filings])
        for filing in filings:
            index, _ = self._get(client, filing.index_url, run_id, ("text/html",))
            urls = parse_document_index(index, filing)
            self._remember_validated(filing.index_url, run_id)
            plans.append((filing, index, urls))
        self._prime_archives(run_id, [url for _, _, urls in plans for url in urls])
        for filing, index, urls in plans:
            for document_url in urls:
                body, _ = self._get(
                    client,
                    document_url,
                    run_id,
                    ("application/pdf",)
                    if document_url.lower().endswith(".pdf")
                    else ("text/html", "text/plain"),
                )
                document = {
                    "filing": {**asdict(filing), "filed_on": filing.filed_on.isoformat()},
                    "url": document_url,
                    "ticker": company.ticker,
                    "exchange": company.exchange,
                    "company_name": company.company_name,
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "retrieved_at": self.captured[document_url][2].isoformat(),
                }
                try:
                    parsed = self._parse_document(body, document, datetime.now(UTC))
                except UnreadableDocumentError:
                    if not self.settings.enable_sec_ocr:
                        raise
                    document["ocr_sha256"] = self._recognize_document(
                        client, run_id, body, document, index, filing.index_url
                    )
                    self.current_url = document_url
                    parsed = self._parse_document(body, document, datetime.now(UTC))
                candidates.extend(parsed)
                self._remember_validated(document_url, run_id)
                documents.append(document)
        return (
            {
                "documents": documents,
                "projection_inputs": inputs,
                "projection_horizon_days": self.settings.publication_horizon_days,
                "references": [{"body_sha256": item[0]} for item in self.captured.values()],
            },
            checked_at,
            candidates,
        )

    def _audit_unused(self, run_id: UUID, results: list[tuple[str, Result]]) -> None:
        if not results:
            return
        refused = None
        for url, result in results:
            for attempt, status in result.attempts:
                self.network_requests += 1
                self.session.add(
                    HttpAttempt(
                        run_id=run_id,
                        endpoint_url=url,
                        attempt_number=attempt,
                        status_class=status,
                        observed_at=result.observed,
                    )
                )
            if result.error and any(status == "4xx" for _, status in result.attempts):
                refused = result.error
            if result.body and result.error is None:
                digest = hashlib.sha256(result.body).hexdigest()
                path = self.service._store_raw(digest, result.body)
                self.session.add(
                    RawObject(
                        run_id=run_id,
                        sha256=digest,
                        byte_size=len(result.body),
                        content_type=result.headers.get("content-type", ""),
                        source_url=url,
                        storage_path=str(path.resolve()),
                        retrieved_at=result.observed,
                        parser_version=NATIVE_PARSER_VERSION,
                    )
                )
        self.session.commit()
        if refused:
            raise refused

    def _prime_archives(self, run_id: UUID, urls: list[str]) -> None:
        if self.archive_prefetch is None:
            return
        self._audit_unused(run_id, self.archive_prefetch.drain())
        requests = []
        references = {}
        now = datetime.now(UTC)
        for url in dict.fromkeys(urls):
            cache = self._endpoint_cache(url)
            if cache and not self.settings.sec_force_recheck:
                if (
                    url not in self.review_urls
                    and timedelta(0) <= now - as_utc(cache.validated_at) < ARCHIVE_RECHECK
                ):
                    continue
                if not re.fullmatch(r"[a-f0-9]{64}", cache.payload_sha256):
                    raise PayloadError("Conditional cache identity is invalid")
                references[url] = CacheReference(
                    self.settings.raw_dir / f"{cache.payload_sha256}.bin",
                    cache.payload_sha256,
                    dict(cache.safe_headers),
                )
            requests.append(url)
        self.archive_prefetch.prime(requests, references)

    def _remember_validated(self, url: str, run_id: UUID) -> None:
        digest, headers, retrieved_at = self.captured[url]
        run = self.session.get(CollectionRun, run_id)
        if run is None:
            raise RuntimeError("collection run disappeared")
        key = hashlib.sha256(url.encode()).hexdigest()
        cache = self._endpoint_cache(url)
        values = dict(
            source_id=run.source_id,
            url=url,
            payload_sha256=digest,
            storage_path=str((self.settings.raw_dir / f"{digest}.bin").resolve()),
            safe_headers=dict(headers),
            validated_at=retrieved_at,
            parser_version=NATIVE_PARSER_VERSION,
        )
        if cache is None:
            cache = EndpointCache(url_hash=key, **values)
            self.session.add(cache)
            self.endpoint_caches[url] = cache
        else:
            unchanged = all(
                (
                    as_utc(cache.validated_at) == value
                    if name == "validated_at"
                    else getattr(cache, name) == value
                )
                for name, value in values.items()
            )
            if unchanged:
                return
            for name, value in values.items():
                setattr(cache, name, value)
        # New HTTP evidence has already been committed by _get. Cache metadata and
        # repeated raw references can be committed together at the company boundary.
        self.session.flush()

    def _recognize_document(
        self,
        client: SafeHttpClient,
        run_id: UUID,
        body: bytes,
        document: dict[str, Any],
        index: bytes,
        index_url: str,
    ) -> str:
        pages = []
        cache_root = self.settings.output_dir.parent / "ocr-auto"
        saved = self.pdf_ocr_proofs.get((document["url"], document["body_sha256"]))
        if saved and not self.settings.sec_force_recheck:
            proof = json.loads(self._body({"body_sha256": saved}))
            prior_pages = proof["pages"]
            if (
                body.startswith(b"%PDF-")
                and proof["document_sha256"] == document["body_sha256"]
                and proof["document_url"] == document["url"]
                and 1 <= len(prior_pages) <= 150
                and [p["number"] for p in prior_pages] == list(range(1, len(prior_pages) + 1))
                and all(
                    p["recognition"]["readable"] and p["recognition"]["context"] == engine_context()
                    for p in prior_pages
                )
            ):
                # Exact PDF bytes/models are unchanged; parse reuse is proof-bound.
                # Verify every retained page even when the derived parse is cached.
                for page in prior_pages:
                    self._body({"body_sha256": page["image_sha256"]})
                self.ocr_evidence[document["url"]] = saved
                return saved
        completed, total, ticker = self.progress_state

        def heartbeat(stage: str) -> None:
            if self.progress:
                self.progress(run_id, completed, total, f"{ticker} · {stage}")

        images: Iterable[tuple[str, bytes]]
        if document["url"].lower().endswith(".pdf"):
            rendered = render_pdf(body, lambda: heartbeat("PDF OCR"))
            total_pages = len(rendered)
            images = ((document["url"], image) for image in rendered)
        else:
            urls = indexed_images(index, body, index_url, document["url"])
            total_pages = len(urls)
            if self.archive_prefetch and not self.archive_prefetch.pending:
                self._prime_archives(run_id, urls)
            images = (
                (url, self._get(client, url, run_id, ("image/jpeg", "image/png"))[0])
                for url in urls
            )
        for number, (url, image_body) in enumerate(images, start=1):
            digest = hashlib.sha256(image_body).hexdigest()
            if url == document["url"]:
                self.service._store_raw(digest, image_body)  # locally rendered PDF page
            result = recognize_image(
                image_body, cache_root, partial(heartbeat, f"OCR {number}/{total_pages}")
            )
            if result["readable"] and url != document["url"]:
                self._remember_validated(url, run_id)
            pages.append(
                {"number": number, "source_url": url, "image_sha256": digest, "recognition": result}
            )
            if self.progress:
                completed, total, ticker = self.progress_state
                self.progress(run_id, completed, total, f"{ticker} · OCR {number}/{total_pages}")
        payload = json.dumps(
            {
                "document_sha256": document["body_sha256"],
                "document_url": document["url"],
                "pages": pages,
            },
            sort_keys=True,
        ).encode()
        digest = hashlib.sha256(payload).hexdigest()
        path = self.service._store_raw(digest, payload)
        self.session.add(
            RawObject(
                run_id=run_id,
                sha256=digest,
                byte_size=len(payload),
                content_type="application/json",
                source_url=document["url"],
                storage_path=str(path.resolve()),
                retrieved_at=datetime.now(UTC),
                parser_version=OCR_VERSION,
            )
        )
        self.session.commit()
        self.ocr_evidence[document["url"]] = digest
        return digest

    def _endpoint_cache(self, url: str) -> EndpointCache | None:
        """Bound metadata references to one company, never cache raw universe bodies."""
        if url not in self.endpoint_caches:
            self.endpoint_caches[url] = self.session.get(
                EndpointCache, hashlib.sha256(url.encode()).hexdigest()
            )
        return self.endpoint_caches[url]

    def _get(
        self,
        client: SafeHttpClient,
        url: str,
        run_id: UUID,
        content_types: tuple[str, ...],
        *,
        refresh_metadata: bool = False,
    ) -> tuple[bytes, httpx.Headers]:
        self.current_url = url
        validate_public_url(url)
        if httpx.URL(url).host not in client.policy.allowed_hosts:
            raise SourcePermissionError("SEC endpoint is outside the approved hosts")

        def audit(attempt: int, status: str) -> None:
            self.network_requests += 1
            self.session.add(
                HttpAttempt(
                    run_id=run_id, endpoint_url=url, attempt_number=attempt, status_class=status
                )
            )
            self.session.commit()

        client.audit = audit
        now = datetime.now(UTC)
        must_recheck = refresh_metadata or url in self.review_urls
        retrieved_at = now
        cache = self._endpoint_cache(url)
        if self.settings.sec_force_recheck:
            cache = None
        cached_body = None
        # Raw bytes are not old-parser facts: all inputs below are validated by the
        # current parser, whose version independently binds the derived parse cache.
        if cache and cache.url == url:
            body = self._body({"body_sha256": cache.payload_sha256})
            if (
                not must_recheck
                and not url.startswith("https://data.sec.gov/submissions/CIK")
                and (timedelta(0) <= now - as_utc(cache.validated_at) < ARCHIVE_RECHECK)
            ):
                cached_body = body
                headers = httpx.Headers(cache.safe_headers)
                retrieved_at = as_utc(cache.validated_at)
            else:
                client.remember_validated(url, body, httpx.Headers(cache.safe_headers))
        if cached_body is None and not must_recheck and not self.settings.sec_force_recheck:
            run = self.session.get(CollectionRun, run_id)
            if run is None:
                raise RuntimeError("collection run disappeared")
            # Recover captures from failed old-parser scans, but do not trust them.
            # Every recovered body goes through the current validation path below.
            retained = self.session.scalar(
                select(RawObject)
                .join(CollectionRun, CollectionRun.id == RawObject.run_id)
                .where(
                    RawObject.source_url == url,
                    RawObject.run_id != run_id,
                    RawObject.parser_version != "sec-history-merge-v1",
                    RawObject.retrieved_at >= now - RESUME_WINDOW,
                    RawObject.retrieved_at <= now,
                    CollectionRun.evidence_mode == run.evidence_mode,
                    CollectionRun.source_id == run.source_id,
                    CollectionRun.state.in_(["FAILED", "RUNNING"]),
                )
                .order_by(RawObject.retrieved_at.desc())
                .limit(1)
            )
            if retained:
                cached_body = self._body({"body_sha256": retained.sha256})
                headers = httpx.Headers({"content-type": retained.content_type})
                retrieved_at = as_utc(retained.retrieved_at)
        try:
            if cached_body is None:
                prefetcher = self.prefetch if refresh_metadata else self.archive_prefetch
                if prefetcher and url in prefetcher.pending:
                    prefetched = prefetcher.take(url)
                    for attempt, status in prefetched.attempts:
                        audit(attempt, status)
                    if prefetched.error:
                        raise prefetched.error
                    body, headers, retrieved_at = (
                        prefetched.body,
                        prefetched.headers,
                        prefetched.observed,
                    )
                else:
                    body, headers = client.get(url)
                    retrieved_at = datetime.now(UTC)
            else:
                body = cached_body
                self.reused_objects += 1
        finally:
            # Persistent cache is on disk; do not retain a full universe's raw bytes in RAM.
            client.cache.pop(url, None)
        if len(body) > self.settings.max_response_bytes:
            raise PayloadTooLargeError("saved response exceeded configured size limit")
        content_type = headers.get("content-type", "").lower()
        digest = hashlib.sha256(body).hexdigest()
        path = (
            self.settings.raw_dir / f"{digest}.bin"
            if cached_body is not None
            or (
                cache is not None
                and digest == cache.payload_sha256
                and digest in self.verified_bodies
            )
            else self.service._store_raw(digest, body)
        )
        self.session.add(
            RawObject(
                run_id=run_id,
                sha256=digest,
                byte_size=len(body),
                content_type=content_type,
                source_url=url,
                storage_path=str(path.resolve()),
                retrieved_at=retrieved_at,
                parser_version=NATIVE_PARSER_VERSION,
            )
        )
        run = self.session.get(CollectionRun, run_id)
        if run:
            run.last_progress_at = datetime.now(UTC)
        if cached_body is None:
            self.session.commit()  # Persist fresh evidence even if parsing later fails.
        self.captured[url] = (digest, headers, retrieved_at)
        if self.progress:
            self.progress(run_id, *self.progress_state)
        if not any(expected in content_type for expected in content_types):
            raise PayloadError("unexpected SEC endpoint content type")
        return body, headers

    def _parse_document(
        self, body: bytes, document: dict[str, Any], observed: datetime
    ) -> list[EventCandidate]:
        digest = hashlib.sha256(body).hexdigest()
        if document.get("body_sha256", digest) != digest:
            raise PayloadError("SEC document comparison checksum mismatch")
        context = {
            name: document[name] for name in ("filing", "url", "ticker", "exchange", "company_name")
        }
        if document.get("ocr_sha256"):
            context["ocr_sha256"] = document["ocr_sha256"]
        key = hashlib.sha256(
            json.dumps(
                {
                    **context,
                    "body_sha256": digest,
                    "parser": NATIVE_PARSER_VERSION,
                    "evidence_mode": self.evidence_mode,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cache = self.parse_caches.get(key)
        if cache is None and self.run_id:
            cache = self.session.get(SecDocumentParse, key)
            if cache is not None:
                self.parse_caches[key] = cache
        if (
            cache
            and cache.parsed_for_date <= observed.date()
            and not self.settings.sec_force_recheck
        ):
            candidates = [EventCandidate.model_validate(item) for item in cache.payload]
            self.reused_parses += 1
        else:
            values = document["filing"]
            filing = Filing(**{**values, "filed_on": date.fromisoformat(values["filed_on"])})
            arguments = dict(
                filing=filing,
                url=document["url"],
                ticker=document["ticker"],
                exchange=document["exchange"],
                company_name=document["company_name"],
                observed=observed,
            )
            if document.get("ocr_sha256"):
                proof = json.loads(self._body({"body_sha256": document["ocr_sha256"]}))
                if proof["document_sha256"] != digest or proof["document_url"] != document["url"]:
                    raise OcrQualityError("OCR evidence does not match this document")
                pages = proof["pages"]
                if not pages or [p["number"] for p in pages] != list(range(1, len(pages) + 1)):
                    raise OcrQualityError("OCR document is incomplete")
                texts: list[list[str]] = [[], []]
                for page in pages:
                    self._body({"body_sha256": page["image_sha256"]})
                    result = page["recognition"]
                    if not result["readable"]:
                        raise OcrQualityError("A document page did not meet OCR quality checks")
                    if result["blank"]:
                        continue
                    layouts = sorted(
                        result["layouts"], key=lambda item: item["mean_confidence"], reverse=True
                    )
                    if len(layouts) < 2:
                        raise OcrQualityError("OCR requires two layout/model readings")
                    for position in range(2):
                        texts[position].append(layouts[position]["text"])
                readings = [
                    parse_exhibit(body, **arguments, extracted_text="\n".join(text))
                    for text in texts
                ]
                if [c.content_hash for c in readings[0]] != [c.content_hash for c in readings[1]]:
                    raise OcrQualityError("OCR layout readings disagree about event facts")
                candidates = readings[0]
                if candidates:
                    # The original OCR approval retained review of extracted event
                    # facts. Matching recognition is supporting evidence, not sign-off.
                    raise OcrQualityError(
                        "OCR event facts require evidence review before acceptance"
                    )
            else:
                candidates = parse_exhibit(body, **arguments)
            if self.run_id and cache is None:
                run = self.session.get(CollectionRun, self.run_id)
                if run is None:
                    raise RuntimeError("collection run disappeared")
                parsed = SecDocumentParse(
                    key=key,
                    run_id=self.run_id,
                    source_id=run.source_id,
                    evidence_mode=self.evidence_mode,
                    parser_version=NATIVE_PARSER_VERSION,
                    body_sha256=digest,
                    source_url=document["url"],
                    parsed_for_date=observed.date(),
                    payload=[item.model_dump(mode="json") for item in candidates],
                )
                self.session.add(parsed)
                self.parse_caches[key] = parsed
        result = []
        for candidate in candidates:
            if candidate.event_date and candidate.event_date < observed.date():
                continue
            candidate.raw_payload_hash = digest
            if document.get("retrieved_at"):
                candidate.collected_at = as_utc(datetime.fromisoformat(document["retrieved_at"]))
            result.append(candidate)
        return result

    def _parse_company(self, data: dict[str, Any], observed: datetime) -> list[EventCandidate]:
        for reference in data.get("references", []):
            self._body(reference)
        return self._parse(
            json.dumps(
                {
                    **data,
                    "complete": True,
                    "companies_scanned": 1,
                    "estimates_enabled": self.settings.enable_sec_estimates,
                }
            ).encode(),
            observed,
        )

    def _body(self, item: dict[str, Any]) -> bytes:
        if "body_sha256" not in item:
            # Retained v2 bundles remain replayable, without changing their original evidence.
            return base64.b64decode(item["body_base64"], validate=True)
        digest = item["body_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PayloadError("SEC bundle contains an invalid raw reference")
        if digest in self.verified_bodies:
            self.verified_bodies.move_to_end(digest)
            return self.verified_bodies[digest]
        path = self.settings.raw_dir / f"{digest}.bin"
        if not path.is_file() or path.stat().st_size > self.settings.max_response_bytes:
            raise PayloadError("SEC bundle raw evidence is absent or exceeds the size limit")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise PayloadError("SEC bundle raw evidence checksum does not match")
        # Bounded, run-local verified bytes. No persisted cache validity extension.
        while self.verified_bodies and self.verified_body_bytes + len(body) > 32_000_000:
            _, expired = self.verified_bodies.popitem(last=False)
            self.verified_body_bytes -= len(expired)
        self.verified_bodies[digest] = body
        self.verified_body_bytes += len(body)
        return body

    def _parse(self, raw: bytes, observed: datetime) -> list[EventCandidate]:
        payload = json.loads(raw)
        if payload.get("complete") is not True or payload.get("companies_scanned", 0) < 1:
            raise PayloadError("incomplete SEC universe scan")
        if (
            payload.get("estimates_enabled")
            and len(payload.get("projection_inputs", [])) != payload["companies_scanned"]
        ):
            raise PayloadError("incomplete SEC estimation input coverage")
        results = []
        for document in payload["documents"]:
            if self.progress and self.run_id:
                self.progress(self.run_id, *self.progress_state[:2], "Validate files")
            candidates = self._parse_document(self._body(document), document, observed)
            results.extend(candidates)
        for company in payload.get("projection_inputs", []):
            if self.progress and self.run_id:
                self.progress(self.run_id, *self.progress_state[:2], "Validate estimates")
            projection = project_next_earnings(
                self._body(company),
                cik=company["cik"],
                ticker=company["ticker"],
                exchange=company["exchange"],
                company_name=company["company_name"],
                observed=observed,
                horizon_days=payload["projection_horizon_days"],
            )
            for candidate in projection.candidates:
                candidate.raw_payload_hash = company.get("body_sha256")
                if company.get("retrieved_at"):
                    candidate.collected_at = as_utc(datetime.fromisoformat(company["retrieved_at"]))
            results.extend(projection.candidates)
        return results
