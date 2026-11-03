"""Operator-invoked SEC image trial. Never used by collectors, publisher or web requests."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.base import (
    AccessBlockedError,
    FetchPolicy,
    PayloadError,
    SafeHttpClient,
    SourcePermissionError,
    ThrottledError,
)
from wtf_ingestion.config import Settings
from wtf_ingestion.persistence.models import (
    Company,
    OperatorAction,
    RefreshJob,
    Source,
    UniverseMembership,
)
from wtf_ingestion.publication.files import replace_atomic
from wtf_ingestion.universe import active_universe

# Exact failed documents already inside the approved fixed universe, not general crawling.
TRIAL_DOCUMENTS = (
    ("JPM", "0000019617", "0000019617-26-000241", "jpmcpressrelease06252026.htm"),
    ("DOC", "0000765880", "0001628280-26-052608", "ex99206302026.htm"),
    ("WYNN", "0001174922", "0001174922-26-000057", "ex991wml2026interimrepor.htm"),
)


def indexed_images(index: bytes, wrapper: bytes, index_url: str, document_url: str) -> list[str]:
    """Require every image to be explicitly indexed in the same SEC filing directory."""
    root = document_url.rsplit("/", 1)[0] + "/"
    parsed = urlsplit(document_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.sec.gov"
        or not re.fullmatch(r"/Archives/edgar/data/\d{1,10}/\d{18}/[A-Za-z0-9_.-]+", parsed.path)
        or parsed.query
        or parsed.fragment
        or not index_url.startswith(root)
    ):
        raise SourcePermissionError("OCR document leaves its approved SEC filing directory")
    table = HTMLParser(index).css_first('table[summary="Document Format Files"]')
    if table is None:
        raise PayloadError("OCR source index document table is missing")
    listed: set[str] = set()
    for row in table.css("tr"):
        cells = row.css("td")
        if len(cells) >= 4 and cells[3].text(strip=True).upper() == "GRAPHIC":
            link = cells[2].css_first("a")
            if link is not None:
                listed.add(urljoin(index_url, link.attributes.get("href", "")))
    images = []
    for node in HTMLParser(wrapper).css("img"):
        url = urljoin(document_url, node.attributes.get("src", ""))
        if (
            not url.startswith(root)
            or url not in listed
            or not re.fullmatch(
                r"[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png)", url[len(root) :], flags=re.IGNORECASE
            )
        ):
            raise SourcePermissionError("OCR image is unindexed or outside its filing directory")
        images.append(url)
    if not 1 <= len(images) <= 150 or len(set(images)) != len(images):
        raise PayloadError("OCR image page count is empty, duplicate or above the trial bound")
    return images


def sample_pages(total: int, limit: int) -> list[int]:
    if not 1 <= total <= 150 or not 1 <= limit <= 150:
        raise ValueError("invalid OCR sample size")
    if total <= limit:
        return list(range(total))
    if limit == 1:
        return [0]
    return sorted({round(position * (total - 1) / (limit - 1)) for position in range(limit)})


def capture_trial(
    session: Session,
    cfg: Settings,
    *,
    sample_limit: int = 3,
    full_document: bool = False,
    reuse_capture: Path | None = None,
) -> Path:
    cfg.assert_live_sec_ready()
    if not 1 <= sample_limit <= 3:
        raise ValueError("trial approval covers at most three sample pages per document")
    source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
    universe = active_universe(session)
    if (
        not source
        or not source.live_enabled
        or source.fixture_only
        or source.permission_status != "approved"
        or not universe
    ):
        raise SourcePermissionError("OCR trial requires enabled SEC access and a fixed universe")
    if session.scalar(select(RefreshJob.id).where(RefreshJob.state.in_(["queued", "running"]))):
        raise SourcePermissionError("Wait for the source refresh before capturing OCR images")
    capture_root = (cfg.output_dir.parent / "ocr-trial").resolve()
    reused_pages: dict[str, tuple[Path, dict[str, Any]]] = {}
    if reuse_capture is not None:
        reuse_capture = reuse_capture.resolve()
        if not reuse_capture.is_relative_to(capture_root) or reuse_capture.name != "capture.json":
            raise SourcePermissionError("OCR reuse must be retained local capture evidence")
        previous = json.loads(reuse_capture.read_text(encoding="utf-8"))
        if previous["universe_version"] != universe.version:
            raise PayloadError("OCR reuse universe does not match")
        for previous_document in previous["documents"]:
            for page in previous_document["pages"]:
                reused_pages[page["url"]] = (reuse_capture.parent, page)
    run_dir = capture_root / str(uuid4())
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "state": "CAPTURING",
        "purpose": "OCR trial only; no accepted events or feed writes",
        "captured_at": datetime.now(UTC).isoformat(),
        "universe_version": universe.version,
        "sample_limit": sample_limit,
        "capture_mode": "complete_document" if full_document else "sample",
        "reused_images": 0,
        "http_attempts": [],
        "documents": [],
    }
    manifest_path = run_dir / "capture.json"

    def persist() -> None:
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        replace_atomic(temporary, manifest_path)

    def fetch(url: str, *, image: bool = False) -> dict[str, Any]:
        cached = reused_pages.get(url) if image else None
        if cached:
            directory, values = cached
            observed = datetime.fromisoformat(values["retrieved_at"])
            if observed.tzinfo is None:
                raise PayloadError("OCR cached image observation needs a timezone")
            if timedelta(0) <= datetime.now(UTC) - observed <= timedelta(hours=24):
                if (
                    not re.fullmatch(r"[a-f0-9]{64}", values["sha256"])
                    or values["file"] != values["sha256"] + ".bin"
                ):
                    raise PayloadError("OCR cached image reference is malformed")
                body = (directory / values["file"]).read_bytes()
                if len(body) > 12_000_000 or hashlib.sha256(body).hexdigest() != values["sha256"]:
                    raise PayloadError("OCR cached image checksum/size mismatch")
                (run_dir / values["file"]).write_bytes(body)
                manifest["reused_images"] += 1
                return {
                    key: values[key]
                    for key in (
                        "url",
                        "sha256",
                        "file",
                        "retrieved_at",
                        "byte_size",
                        "content_type",
                    )
                }
        body, headers = client.get(url)
        digest = hashlib.sha256(body).hexdigest()
        destination = run_dir / (digest + ".bin")
        if not destination.exists():
            destination.write_bytes(body)
        return {
            "url": url,
            "sha256": digest,
            "file": destination.name,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "byte_size": len(body),
            "content_type": headers.get("content-type", ""),
        }

    client = SafeHttpClient(
        FetchPolicy(
            frozenset({"www.sec.gov"}),
            cfg.sec_user_agent or "",
            max_response_bytes=12_000_000,
            min_interval_seconds=0.5,
            stop_on_throttle=True,
        )
    )
    client.audit = lambda attempt, status: manifest["http_attempts"].append(
        {"attempt": attempt, "status_class": status}
    )
    persist()
    try:
        for ticker, cik, accession, name in TRIAL_DOCUMENTS:
            if not session.scalar(
                select(Company.id)
                .join(UniverseMembership)
                .where(Company.cik == cik, UniverseMembership.universe_version_id == universe.id)
            ):
                raise SourcePermissionError("OCR trial company is absent from the active universe")
            root = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
            )
            index = fetch(root + accession + "-index.html")
            wrapper = fetch(root + name)
            if any("text/html" not in entry["content_type"] for entry in (index, wrapper)):
                raise PayloadError("OCR trial index/wrapper is not HTML")
            images = indexed_images(
                (run_dir / index["file"]).read_bytes(),
                (run_dir / wrapper["file"]).read_bytes(),
                index["url"],
                wrapper["url"],
            )
            document: dict[str, Any] = {
                "ticker": ticker,
                "cik": cik,
                "accession": accession,
                "index": index,
                "wrapper": wrapper,
                "total_pages": len(images),
                "complete_capture": False,
                "pages": [],
            }
            manifest["documents"].append(document)
            positions = (
                list(range(len(images)))
                if full_document
                else sample_pages(len(images), sample_limit)
            )
            for position in positions:
                image = fetch(images[position], image=True)
                if not any(mime in image["content_type"] for mime in ("image/jpeg", "image/png")):
                    raise PayloadError("OCR trial image has an unexpected content type")
                document["pages"].append({"page_number": position + 1, **image})
                persist()
            document["complete_capture"] = len(document["pages"]) == len(images)
            persist()
        manifest["state"] = "CAPTURED_REVIEW_ONLY"
    except (AccessBlockedError, ThrottledError):
        source.live_enabled = False
        session.add(
            OperatorAction(
                action_type="ocr_trial_access_incident",
                actor="local-ocr-trial",
                target_id="sec_edgar",
                reason="SEC blocked/throttled OCR capture; source disabled, review required",
                idempotency_key=f"ocr-block-{run_dir.name}",
            )
        )
        session.commit()
        manifest["state"] = "ACCESS_BLOCKED_SOURCE_DISABLED"
        raise
    except Exception:
        manifest["state"] = "FAILED_REVIEW_ONLY"
        raise
    finally:
        client.close()
        persist()
    return manifest_path
