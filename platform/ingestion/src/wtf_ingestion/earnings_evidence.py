"""Immutable, bounded fact packs from accepted evidence; no network or LLM calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from wtf_ingestion.adapters.base import SourceError
from wtf_ingestion.adapters.parsing import ANNOUNCEMENT, announcement_date, normalize_text
from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.adapters.sec_estimates import project_next_earnings
from wtf_ingestion.adapters.sec_native import (
    Filing,
    filing_document_text,
    parse_exhibit,
)
from wtf_ingestion.config import Settings
from wtf_ingestion.earnings import earnings_data, read_snapshot
from wtf_ingestion.persistence.locking import pipeline_lock
from wtf_ingestion.persistence.models import PublicationManifest, UniverseVersion

PACK_VERSION = "earnings-evidence-v1"
MAX_PACK_BYTES = 10_000_000


class EvidenceUnavailable(RuntimeError):
    code = "EARNINGS_EVIDENCE_UNAVAILABLE"


def encoded(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def retained_body(cfg: Settings, digest: str) -> bytes:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise EvidenceUnavailable("Invalid retained evidence identity.")
    root = cfg.raw_dir.resolve()
    path = (root / f"{digest}.bin").resolve()
    if path.parent != root:
        raise EvidenceUnavailable("Retained evidence escapes its configured root.")
    try:
        if not path.is_file() or path.stat().st_size > cfg.max_response_bytes:
            raise EvidenceUnavailable("Retained evidence exceeds the byte bound.")
        body = path.read_bytes()
    except OSError as exc:
        raise EvidenceUnavailable("Retained evidence is unavailable.") from exc
    if len(body) > cfg.max_response_bytes:
        raise EvidenceUnavailable("Retained evidence exceeds the byte bound.")
    if hashlib.sha256(body).hexdigest() != digest:
        raise EvidenceUnavailable("Retained evidence checksum failed.")
    return body


def document_evidence(
    row: dict[str, Any], cfg: Settings, bundles: dict[str, dict[str, Any]], *, fixture: bool
) -> dict[str, Any]:
    event = row["event"]
    evidence = row["evidence"][0]
    observed = datetime.fromisoformat(evidence["observed_at"])
    descriptor = {
        **evidence,
        "source_title": event["title"],
        "publisher": event["company_name"],
        "hosting_source": "SEC EDGAR",
        "audience": "internal",
        "rights_policy": "docs/sources/SEC.md",
        "source_published_at": None,
        "source_publication_precision": "unknown",
    }
    if row["estimate"] is not None:
        estimate = row["estimate"]
        digest = estimate["estimate_basis_raw_sha256"]
        raw = retained_body(cfg, digest)
        projected = project_next_earnings(
            raw,
            cik=event["cik"],
            ticker=event["ticker"],
            exchange=event["exchange"],
            company_name=event["company_name"],
            observed=observed,
            horizon_days=estimate["projection_horizon_days"],
        ).candidates
        if (
            len(projected) != 1
            or projected[0].content_hash != event["content_hash"]
            or projected[0].metadata != estimate
        ):
            raise EvidenceUnavailable("Estimate input does not match its accepted filing basis.")
        return {
            **descriptor,
            "kind": "filing_metadata_projection",
            "parser_input_sha256": digest,
            "document_sha256": None,
            "excerpt": None,
            "filed_on": estimate["estimate_basis_date"],
            "filing_date_precision": "day",
            "calculation": {
                "method": estimate["estimate_method"],
                "input_filing_date": estimate["estimate_basis_date"],
                "weeks_added": 52,
                "output_estimated_date": event["event_date"],
                "actual_release_date": False,
            },
        }
    digest = evidence["raw_sha256"]
    if digest not in bundles:
        bundles[digest] = json.loads(retained_body(cfg, digest))
    bundle = bundles[digest]
    if fixture and bundle.get("fixture") is True:
        candidates = SecEarningsAdapter(cfg).parse_fixture(encoded(bundle), observed)
        if not any(candidate.content_hash == event["content_hash"] for candidate in candidates):
            raise EvidenceUnavailable("Fixture evidence and accepted facts disagree.")
        return {
            **descriptor,
            "kind": "fixture_calendar",
            "document_sha256": digest,
            "excerpt": None,
        }
    if (
        event["source_id"] != "sec_edgar"
        or bundle.get("complete") is not True
        or not isinstance(bundle.get("documents"), list)
    ):
        raise EvidenceUnavailable("Accepted source bundle is incomplete.")
    documents = [entry for entry in bundle["documents"] if entry.get("url") == event["source_url"]]
    if len(documents) != 1:
        raise EvidenceUnavailable("Exact published source document is not unique in its bundle.")
    document = documents[0]
    filing_data = document["filing"]
    filing = Filing(**{**filing_data, "filed_on": date.fromisoformat(filing_data["filed_on"])})
    if filing.cik != event["cik"] or any(
        document[name] != event[name] for name in ("ticker", "exchange", "company_name")
    ):
        raise EvidenceUnavailable("Source document and issuer identity disagree.")
    body = retained_body(cfg, document["body_sha256"])
    if document.get("ocr_sha256"):
        # The collector itself refuses positive OCR-derived facts pending review.
        # A pack must not create an alternative acceptance path around that gate.
        raise EvidenceUnavailable("OCR research-pack extraction requires its qualified page proof.")
    candidates = parse_exhibit(
        body,
        filing=filing,
        url=event["source_url"],
        ticker=event["ticker"],
        exchange=event["exchange"],
        company_name=event["company_name"],
        observed=observed,
    )
    matching = [
        candidate for candidate in candidates if candidate.content_hash == event["content_hash"]
    ]
    if len(matching) != 1 or matching[0].metadata.get("conference_call") != row["conference_call"]:
        raise EvidenceUnavailable(
            "Source document does not reproduce accepted calendar/call facts."
        )
    normalized = normalize_text(filing_document_text(body, event["source_url"]))
    statements = [
        match
        for match in ANNOUNCEMENT.finditer(normalized)
        if announcement_date(match).isoformat() == event["event_date"]
    ]
    if not statements:
        raise EvidenceUnavailable("Forward statement locator is unavailable.")
    start = statements[0].start()
    end = min(len(normalized), statements[0].end() + 1000)
    return {
        **descriptor,
        "kind": "official_forward_statement",
        "document_sha256": document["body_sha256"],
        "source_bundle_sha256": digest,
        "filed_on": filing.filed_on.isoformat(),
        "filing_date_precision": "day",
        "normalized_text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "locator": {"kind": "normalized_text_characters", "start": start, "end": end},
        "excerpt": normalized[start:end],
    }


def claims_for(row: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_id = row["evidence"][0]["evidence_id"]
    claims = [
        {
            "claim_id": f"{row['event']['event_id']}:{name}",
            "field": name,
            "value": field["value"],
            "support_state": field["state"],
            "evidence_ids": [evidence_id],
            "kind": "calculated_estimate" if field["state"] == "estimated" else "source_fact",
        }
        for name, field in row["fields"].items()
        if field["value"] is not None
    ]
    if row["conference_call"]["state"] == "available":
        claims.append(
            {
                "claim_id": f"{row['event']['event_id']}:conference_call",
                "field": "conference_call",
                "value": row["conference_call"],
                "support_state": "available",
                "evidence_ids": [evidence_id],
                "kind": "source_fact",
            }
        )
    return claims


def pack_directory(cfg: Settings, publication_id: str) -> Path:
    root = (cfg.output_dir.parent / "research-packs").resolve()
    directory = (root / publication_id / PACK_VERSION).resolve()
    if directory.parent.parent != root:
        raise EvidenceUnavailable("Evidence pack directory failed containment validation.")
    return directory


def identity_basis(universe: UniverseVersion) -> dict[str, Any]:
    return {
        "kind": "fixed_universe_membership",
        "universe_version": universe.version,
        "source_identity": universe.source_url,
        "assumption": universe.assumption,
        "fields": ["company_id", "company_name", "cik", "ticker", "exchange", "listings"],
    }


def _read_pack_files(
    cfg: Settings, publication: PublicationManifest
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    directory = pack_directory(cfg, str(publication.id))
    paths = (directory / "pack.json", directory / "manifest.json")
    for path, limit in zip(paths, (MAX_PACK_BYTES, 10_000), strict=True):
        if path.resolve().parent != directory or not path.is_file() or path.stat().st_size > limit:
            raise EvidenceUnavailable("Evidence pack files are missing or exceed their bounds.")
    body = paths[0].read_bytes()
    payload = json.loads(body)
    manifest = json.loads(paths[1].read_bytes())
    if (
        manifest["sha256"] != hashlib.sha256(body).hexdigest()
        or manifest["publication_manifest_sha256"] != publication.manifest_sha256
        or payload["publication_manifest_sha256"] != publication.manifest_sha256
        or payload["publication_id"] != str(publication.id)
        or manifest["publication_id"] != str(publication.id)
        or payload["evidence_mode"] != publication.evidence_mode
        or payload["universe_version_id"] != str(publication.universe_version_id)
        or payload["contract_version"] != "earnings-evidence-0.1.0"
        or payload["builder_version"] != PACK_VERSION
        or manifest["builder_version"] != PACK_VERSION
        or manifest["records"] != len(payload["records"])
    ):
        raise EvidenceUnavailable("Evidence pack checksum/identity/version failed validation.")
    built = datetime.fromisoformat(manifest["built_at"])
    if built.tzinfo is None or built > datetime.now(UTC):
        raise EvidenceUnavailable("Evidence pack build clock is invalid.")
    return payload, manifest, body


def read_evidence_pack(
    session: Session, cfg: Settings, *, retained: bool = False
) -> dict[str, Any]:
    """Read the exact active publication's pack without fetching or generating data."""
    try:
        publication, _ = read_snapshot(session, cfg)
        research = earnings_data(session, cfg, retained=retained)
        payload, manifest, _ = _read_pack_files(cfg, publication)
        universe = session.get(UniverseVersion, publication.universe_version_id)
        if (
            universe is None
            or payload["identity_basis"] != identity_basis(universe)
            or payload["publication_generated_at"] != research["publication_generated_at"]
            or payload["briefs_generated"] is not False
            or payload["prices_included"] is not False
        ):
            raise EvidenceUnavailable("Evidence pack scope and accepted publication disagree.")
        expected = {row["event"]["event_id"]: row for row in research["data"]}
        if len(payload["records"]) != len(expected) or len(
            {row["event"]["event_id"] for row in payload["records"]}
        ) != len(expected):
            raise EvidenceUnavailable("Evidence pack event coverage failed validation.")
        bundles: dict[str, dict[str, Any]] = {}
        for item in payload["records"]:
            row = expected[item["event"]["event_id"]]
            pack_id = item["evidence_pack_id"]
            material = {key: value for key, value in item.items() if key != "evidence_pack_id"}
            if (
                hashlib.sha256(encoded(material)).hexdigest() != pack_id
                or item["event"] != row["event"]
                or item["company_id"] != row["company_id"]
                or item["listings"] != row["listings"]
                or item["fiscal"] != row["fiscal"]
                or item["claims"] != claims_for(row)
                or item["evidence"]
                != [
                    document_evidence(
                        row, cfg, bundles, fixture=publication.evidence_mode == "fixture"
                    )
                ]
                or item["unavailable_fields"]
                != {
                    name: field["reason"]
                    for name, field in row["fields"].items()
                    if field["value"] is None
                }
            ):
                raise EvidenceUnavailable("Evidence pack and exact accepted facts disagree.")
        if payload["missing_issuers"] != sorted(
            research["missing_issuers"], key=lambda row: row["cik"]
        ):
            raise EvidenceUnavailable("Evidence pack issuer coverage failed validation.")
        return {
            **payload,
            "manifest": manifest,
            "as_of": research["as_of"],
            "calendar_state": research["calendar_state"],
            "is_current": research["is_current"],
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SourceError) as exc:
        raise EvidenceUnavailable("Evidence pack is unavailable or malformed.") from exc


def publish_evidence_pack(session: Session, cfg: Settings) -> dict[str, Any]:
    with pipeline_lock(session):
        try:
            return _publish_evidence_pack(session, cfg)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, SourceError) as exc:
            raise EvidenceUnavailable("Evidence pack validation/publication failed.") from exc


def _publish_evidence_pack(session: Session, cfg: Settings) -> dict[str, Any]:
    publication, _ = read_snapshot(session, cfg)
    research = earnings_data(session, cfg)
    bundles: dict[str, dict[str, Any]] = {}
    rows = []
    for row in research["data"]:
        evidence = document_evidence(
            row, cfg, bundles, fixture=publication.evidence_mode == "fixture"
        )
        item = {
            "event": row["event"],
            "company_id": row["company_id"],
            "listings": row["listings"],
            "fiscal": row["fiscal"],
            "evidence": [evidence],
            "claims": claims_for(row),
            "unavailable_fields": {
                name: field["reason"]
                for name, field in row["fields"].items()
                if field["value"] is None
            },
        }
        item["evidence_pack_id"] = hashlib.sha256(encoded(item)).hexdigest()
        rows.append(item)
    universe = session.get(UniverseVersion, publication.universe_version_id)
    if universe is None:
        raise EvidenceUnavailable("Evidence pack universe binding is missing.")
    payload = {
        "contract_version": "earnings-evidence-0.1.0",
        "builder_version": PACK_VERSION,
        "publication_id": str(publication.id),
        "publication_manifest_sha256": publication.manifest_sha256,
        "publication_generated_at": research["publication_generated_at"],
        "evidence_mode": publication.evidence_mode,
        "universe_version_id": str(publication.universe_version_id),
        "identity_basis": identity_basis(universe),
        "records": sorted(rows, key=lambda row: row["event"]["event_id"]),
        "missing_issuers": sorted(research["missing_issuers"], key=lambda row: row["cik"]),
        "briefs_generated": False,
        "prices_included": False,
    }
    body = encoded(payload)
    digest = hashlib.sha256(body).hexdigest()
    if len(body) > MAX_PACK_BYTES:
        raise EvidenceUnavailable("Evidence pack exceeds its byte bound.")
    directory = pack_directory(cfg, str(publication.id))
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        _, manifest, previous = _read_pack_files(cfg, publication)
        if previous != body:
            raise EvidenceUnavailable("An existing evidence pack cannot be overwritten.")
        return {**manifest, "reused": True}
    temporary = directory.parent / f".pack-{uuid4().hex}"
    temporary.mkdir()
    manifest = {
        "publication_id": str(publication.id),
        "publication_manifest_sha256": publication.manifest_sha256,
        "builder_version": PACK_VERSION,
        "sha256": digest,
        "records": len(rows),
        "built_at": datetime.now(UTC).isoformat(),
    }
    for filename, content in (("pack.json", body), ("manifest.json", encoded(manifest))):
        with (temporary / filename).open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    temporary.rename(directory)
    return {**manifest, "reused": False}
