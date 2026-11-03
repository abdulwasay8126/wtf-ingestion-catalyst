"""Actual retained SEC releases and reproducible descriptive adjusted-price moves."""

from __future__ import annotations

import json
import re
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.parsing import DATE_PATTERN, normalize_text
from wtf_ingestion.adapters.sec_native import Filing, filing_document_text
from wtf_ingestion.config import Settings
from wtf_ingestion.earnings import earnings_data, read_snapshot
from wtf_ingestion.market_data import atomic, bounded, digest, encoded, read_market, scope_from_feed
from wtf_ingestion.persistence.models import RawObject

CONTRACT = "earnings-history-0.1.0"
METHOD = "adjusted-close-release-bracket-v1"
RESULT = (
    r"(?:financial\s+results|operating\s+results|results\s+of\s+operations|net\s+income"
    r"|earnings\s+(?:release|press\s+release|for\b)|quarter(?:ly)?\s+results"
    r"|quarter[^.!?]{0,35}\bresults)"
    r"|results\s+for\s+(?:the\s+)?(?:quarter|fiscal|first|second|third|fourth)\b"
)
PAST = r"(?:issued|released|reported|announced|published)"
DIRECT = re.compile(rf"(?i)\bOn\s+{DATE_PATTERN},?\s+.{{0,200}}?\b{PAST}\b")
INDIRECT = re.compile(
    rf"(?i)\b(?:press|news|earnings)\s+release\b.{{0,60}}?\b(?:was\s+)?"
    rf"(?:issued|released|published)\s+on\s+{DATE_PATTERN}"
)
DATED = re.compile(rf"(?i)\b(?:press|news|earnings)\s+release\b.{{0,80}}?\bdated\s+{DATE_PATTERN}")
IDENTITY_ACTIONS = {
    "name_changes",
    "stock_mergers",
    "cash_mergers",
    "stock_and_cash_mergers",
    "redemptions",
    "worthless_removals",
    "spin_offs",
    "reorganizations",
}
_parse_cache: OrderedDict[tuple[str, str, date, datetime], str] = OrderedDict()
_parse_lock = Lock()


class HistoryUnavailable(RuntimeError):
    pass


def history_body(cfg: Settings, sha: str, root: Path) -> bytes:
    """Hash every body; resolve the trusted root once per complete interpretation.

    The fixed hexadecimal filename cannot traverse. Refuse symlink bodies rather
    than resolving every parent over the Windows bind mount hundreds of times.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise HistoryUnavailable("Invalid retained historical evidence identity.")
    path = root / (sha + ".bin")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > cfg.max_response_bytes:
        raise HistoryUnavailable("Retained historical evidence is unavailable or unbounded.")
    raw = path.read_bytes()
    if len(raw) > cfg.max_response_bytes or digest(raw) != sha:
        raise HistoryUnavailable("Retained historical evidence checksum failed.")
    return raw


def actual_releases(text: str, filed_on: date, observed: datetime) -> list[dict[str, Any]]:
    """Only explicitly dated actual disclosure statements; no filing-date fallback."""
    normalized = normalize_text(text)
    results: dict[str, dict[str, Any]] = {}
    for pattern in (DIRECT, INDIRECT, DATED):
        for match in pattern.finditer(normalized):
            from dateutil.parser import parse

            day = parse(match.group("date"), fuzzy=False).date()
            if not day <= filed_on <= observed.date():
                continue
            start = match.start() if pattern is DIRECT else max(0, match.start() - 400)
            end = min(len(normalized), match.end() + 550)
            following = re.search(r"[.!?](?=\s+[A-Z]|\s*$)", normalized[match.end() : end])
            if following and pattern is DIRECT:
                end = match.end() + following.end()
            clause = normalized[start:end]
            if not re.search(RESULT, clause, re.I) or re.search(
                r"(?i)\b(?:will|plans\s+to|expects\s+to|scheduled\s+to)\s+"
                r"(?:release|report|announce|publish)\b|\b(?:earnings|results)\s+guidance\b",
                clause,
            ):
                continue
            if day.isoformat() in results:
                continue
            session = "UNKNOWN"
            timing = re.split(r"(?i)\b(?:conference\s+call|webcast)\b", clause)[0]
            if re.search(r"(?i)\b(?:before|prior\s+to)\s+(?:the\s+)?market\s+open\b", timing):
                session = "BMO"
            elif re.search(r"(?i)\b(?:after|following)\s+(?:the\s+)?market\s+close\b", timing):
                session = "AMC"
            period = re.search(
                rf"(?i)\b(?:(?:first|second|third|fourth)[ -]quarter[^.!?]{{0,30}}20\d{{2}}"
                rf"|(?:fiscal\s+)?(?:quarter|year|period)\s+ended\s+{DATE_PATTERN})",
                clause,
            )
            results[day.isoformat()] = {
                "actual_release_date": day.isoformat(),
                "session": session,
                "time_precision": "date_only",
                "actual_release_at": None,
                "fiscal_period": period.group(0) if period else None,
                "release_kind": "preliminary_results"
                if "preliminary" in clause.lower()
                else "earnings_results",
                "locator": {"kind": "normalized_text_characters", "start": start, "end": end},
                "excerpt": clause,
                "normalized_text_sha256": digest(normalized.encode()),
            }
    return sorted(results.values(), key=lambda row: row["actual_release_date"])


def cover_identity(raw: bytes, cik: str) -> list[str]:
    """Read source-stated registered share symbols; refuse a conflicting cover CIK."""
    tree = HTMLParser(raw)
    identifiers: set[str] = set()
    tickers: set[str] = set()
    for node in tree.css("[name]"):
        name = (node.attributes.get("name") or "").lower()
        value = node.text(strip=True)
        if name == "dei:entitycentralindexkey":
            identifiers.add(value.zfill(10))
        if name == "dei:tradingsymbol" and re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,14}", value):
            tickers.add(value.upper())
    if identifiers and identifiers != {cik.zfill(10)}:
        raise HistoryUnavailable("SEC cover and retained issuer identity disagree.")
    return sorted(tickers)


def extract_history(cfg: Settings, bundle_sha: str, scope: list[dict[str, Any]]) -> dict[str, Any]:
    raw_root = cfg.raw_dir.resolve()
    bundle = json.loads(history_body(cfg, bundle_sha, raw_root))
    if (
        bundle.get("complete") is not True
        or bundle.get("bundle_format") != "raw_sha256_refs_v1"
        or not isinstance(bundle.get("documents"), list)
        or len(bundle["documents"]) > 20_000
    ):
        raise HistoryUnavailable("Historical SEC bundle is incomplete or unsupported.")
    issuers = {row["cik"] for row in scope}
    assertions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    exclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document in bundle["documents"]:
        filing_data = document["filing"]
        if (
            document["url"].rsplit("/", 1)[-1] != filing_data["primary_document"]
            or "2.02" not in filing_data.get("items", "")
            or filing_data["cik"] not in issuers
        ):
            continue
        if document["url"] in seen:
            raise HistoryUnavailable("Retained primary document identities are duplicated.")
        seen.add(document["url"])
        if filing_data["form"] != "8-K" or document.get("ocr_sha256"):
            exclusions.append(
                {"source_url": document["url"], "reason": "amendment_or_ocr_requires_review"}
            )
            continue
        filing = Filing(**{**filing_data, "filed_on": date.fromisoformat(filing_data["filed_on"])})
        if document["url"] != filing.root_url + filing.primary_document:
            exclusions.append(
                {"source_url": document["url"], "reason": "cofiler_identity_requires_review"}
            )
            continue
        raw = history_body(cfg, document["body_sha256"], raw_root)
        observed = datetime.fromisoformat(document["retrieved_at"])
        # Always verify retained bytes first; cache only their deterministic interpretation.
        cache_key = (document["body_sha256"], document["url"], filing.filed_on, observed)
        with _parse_lock:
            cached = _parse_cache.get(cache_key)
        if cached is None:
            try:
                parsed = {
                    "facts": actual_releases(
                        filing_document_text(raw, document["url"]), filing.filed_on, observed
                    ),
                    "symbols": cover_identity(raw, filing.cik),
                }
            except HistoryUnavailable:
                exclusions.append(
                    {"source_url": document["url"], "reason": "conflicting_cover_issuer_identity"}
                )
                continue
            cached = json.dumps(parsed)
            with _parse_lock:
                _parse_cache[cache_key] = cached
                if len(_parse_cache) > 1024:
                    _parse_cache.popitem(last=False)
        parsed = json.loads(cached)
        facts, symbols = parsed["facts"], parsed["symbols"]
        if not facts:
            exclusions.append(
                {"source_url": document["url"], "reason": "no_supported_actual_release_statement"}
            )
        for fact in facts:
            proof = {
                **fact,
                "source_url": document["url"],
                "document_sha256": document["body_sha256"],
                "observed_at": observed.isoformat(),
                "filed_on": filing.filed_on.isoformat(),
                "accession": filing.accession,
                "cover_tickers": symbols,
            }
            assertions.setdefault((filing.cik, fact["actual_release_date"]), []).append(proof)
    events = []
    for (cik, day), proofs in sorted(assertions.items()):
        sessions = {item["session"] for item in proofs} - {"UNKNOWN"}
        periods = {item["fiscal_period"] for item in proofs if item["fiscal_period"]}
        conflict = len(sessions) > 1 or len(periods) > 1
        events.append(
            {
                "historical_event_id": digest(encoded({"cik": cik, "date": day})),
                "cik": cik,
                "actual_release_date": day,
                "session": next(iter(sessions)) if len(sessions) == 1 else "UNKNOWN",
                "fiscal_period": next(iter(periods)) if len(periods) == 1 else None,
                "time_precision": "date_only",
                "actual_release_at": None,
                "release_kind": proofs[0]["release_kind"],
                "support_state": "CONFLICT" if conflict else "SUPPORTED_ACTUAL",
                "source_assertions": proofs,
            }
        )
    return {"events": events, "exclusions": exclusions, "primary_documents_checked": len(seen)}


def price_reaction(
    event: dict[str, Any],
    listing: dict[str, Any],
    market: dict[str, Any],
    price_index: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    result = {
        "historical_event_id": event["historical_event_id"],
        "ticker": listing["ticker"],
        "listing": listing,
        "method_version": METHOD,
        "state": "UNAVAILABLE",
        "return_percent": None,
        "reason_code": None,
    }
    if event["support_state"] != "SUPPORTED_ACTUAL":
        return {**result, "reason_code": "conflicting_release_evidence"}
    matching = [p for p in event["source_assertions"] if listing["ticker"] in p["cover_tickers"]]
    if not matching or event["cik"] != listing["cik"]:
        return {**result, "reason_code": "historical_listing_identity_not_verified"}
    days = sorted(row["date"] for row in market["trading_calendar"])
    if len(days) != len(set(days)):
        raise HistoryUnavailable("Trading-calendar dates are duplicated.")
    day = event["actual_release_date"]
    preceding, following = bisect_left(days, day) - 1, bisect_right(days, day)
    if not days or day < days[0] or day > days[-1]:
        return {**result, "reason_code": "release_outside_complete_price_window"}
    if (preceding < 0 and event["session"] != "AMC") or (
        following >= len(days) and event["session"] != "BMO"
    ):
        return {**result, "reason_code": "release_outside_complete_price_window"}
    baseline = days[max(0, preceding)]
    measurement = days[min(len(days) - 1, following)]
    window = "preceding_to_following_session_date_bracket"
    if day in days and event["session"] == "BMO":
        measurement, window = day, "preceding_close_to_release_day_close"
    elif day in days and event["session"] == "AMC":
        baseline, window = day, "release_day_close_to_following_close"
    for action in market["corporate_actions"]:
        if listing["ticker"] in action["tickers"] and action["type"] in IDENTITY_ACTIONS:
            return {**result, "reason_code": "corporate_identity_action_requires_review"}
    prices = [
        row
        for row in (price_index[listing["ticker"]] if price_index is not None else market["prices"])
        if row["ticker"] == listing["ticker"] and baseline <= row["trading_date"] <= measurement
    ]
    expected = {d for d in days if baseline <= d <= measurement}
    if len(prices) != len(expected) or {p["trading_date"] for p in prices} != expected:
        return {**result, "reason_code": "missing_or_duplicate_window_prices"}
    first = next(p for p in prices if p["trading_date"] == baseline)
    last = next(p for p in prices if p["trading_date"] == measurement)
    if any(
        p.get("adjustment") != "all" or p.get("currency") != "USD" or p.get("feed") != "sip"
        for p in prices
    ):
        return {**result, "reason_code": "incompatible_price_adjustments"}
    try:
        with localcontext() as ctx:
            ctx.prec = 40
            start, end = Decimal(first["close"]), Decimal(last["close"])
            if not start.is_finite() or not end.is_finite() or start <= 0 or end <= 0:
                raise InvalidOperation
            percent = format(((end / start - 1) * 100).quantize(Decimal("0.000001")), "f")
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return {**result, "reason_code": "invalid_or_nonpositive_close"}
    return {
        **result,
        "state": "AVAILABLE",
        "window_kind": window,
        "baseline_date": baseline,
        "measurement_date": measurement,
        "baseline_close": first["close"],
        "measurement_close": last["close"],
        "return_percent": percent,
        "currency": "USD",
        "adjustment": "all",
        "price_evidence_ids": sorted({p["evidence_id"] for p in prices}),
        "source_assertion_sha256": digest(encoded(matching)),
        "identity_basis": "sec_cik_registered_share_symbol_and_provider_asof_prices",
        "caveat": (
            "Descriptive adjusted-price move; no causal or predictive claim. "
            "Unknown sessions use a wider date bracket."
        ),
    }


def history_root(cfg: Settings) -> Path:
    return cfg.output_dir.resolve().parent / "earnings-history"


def indexed_prices(market: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {s["ticker"]: [] for s in market["scope"]}
    for row in market["prices"]:
        result[row["ticker"]].append(row)
    return result


def publish_history(session: Session, cfg: Settings) -> dict[str, Any]:
    """Caller holds pipeline lease; use only raw inputs belonging to the accepted calendar."""
    publication, _ = read_snapshot(session, cfg)
    feed = earnings_data(session, cfg)
    scope = scope_from_feed(feed)
    market = read_market(
        cfg,
        calendar_publication_id=str(publication.id),
        calendar_manifest_sha256=publication.manifest_sha256,
    )
    if not market["is_current"] or not market["data"]:
        raise HistoryUnavailable("A healthy source-verified market publication is required.")
    if market["data"]["scope"] != scope:
        raise HistoryUnavailable("Historical market and calendar scopes differ.")
    raws = session.scalars(
        select(RawObject)
        .where(
            RawObject.run_id.in_([UUID(v) for v in publication.included_run_ids]),
            RawObject.source_url == "https://data.sec.gov/submissions/",
        )
        .order_by(RawObject.retrieved_at.desc())
        .limit(1)
    ).all()
    if len(raws) != 1:
        raise HistoryUnavailable("Accepted calendar SEC bundle is not available.")
    root, identifier = history_root(cfg), str(uuid4())
    atomic(root / "status.json", {"state": "BUILDING", "attempt_id": identifier})
    try:
        history = extract_history(cfg, raws[0].sha256, scope)
        if not history["events"]:
            raise HistoryUnavailable("No supported actual historical earnings release was found.")
        price_index = indexed_prices(market["data"])
        reactions = [
            price_reaction(event, listing, market["data"], price_index)
            for event in history["events"]
            for listing in scope
            if event["cik"] == listing["cik"]
        ]
        pack = {
            "contract_version": CONTRACT,
            "history_publication_id": identifier,
            "calendar_publication_id": str(publication.id),
            "calendar_manifest_sha256": publication.manifest_sha256,
            "market_publication_id": market["data"]["market_publication_id"],
            "source_bundle_sha256": raws[0].sha256,
            "scope": scope,
            "scope_sha256": digest(encoded(scope)),
            "evidence_mode": publication.evidence_mode,
            "method_version": METHOD,
            "computed_at": datetime.now(UTC).isoformat(),
            "market_trust_deadline": market["data"]["trust_deadline"],
            "history_scope": (
                "Explicit release statements in retained native SEC Item 2.02 primary filings. "
                "Coverage is bounded by retained capture; no exhaustive/two-year backfill claim."
            ),
            **history,
            "reactions": reactions,
            "coverage": [
                {
                    "ticker": listing["ticker"],
                    "actual_events": sum(e["cik"] == listing["cik"] for e in history["events"]),
                    "available_reactions": sum(
                        r["ticker"] == listing["ticker"] and r["state"] == "AVAILABLE"
                        for r in reactions
                    ),
                }
                for listing in scope
            ],
        }
        raw = encoded(pack)
        if len(raw) > 12_000_000:
            raise HistoryUnavailable("Historical publication exceeds its byte bound.")
        staging = root / "publications" / (identifier + ".staging")
        staging.mkdir(parents=True, exist_ok=False)
        atomic(staging / "data.json", pack)
        manifest = {
            "contract_version": CONTRACT,
            "history_publication_id": identifier,
            "data_sha256": digest(raw),
            "calendar_publication_id": str(publication.id),
            "market_publication_id": pack["market_publication_id"],
        }
        atomic(staging / "manifest.json", manifest)
        staging.rename(staging.with_name(identifier))
        atomic(root / "status.json", {"state": "SUCCEEDED", "history_publication_id": identifier})
        atomic(
            root / "current.json",
            {"history_publication_id": identifier, "manifest_sha256": digest(encoded(manifest))},
        )
        return {
            "history_publication_id": identifier,
            "actual_events": len(history["events"]),
            "issuers_with_history": len({e["cik"] for e in history["events"]}),
            "available_reactions": sum(r["state"] == "AVAILABLE" for r in reactions),
            "excluded_documents": len(history["exclusions"]),
        }
    except (HistoryUnavailable, OSError, ValueError, KeyError):
        atomic(
            root / "status.json",
            {
                "state": "FAILED",
                "attempt_id": identifier,
                "failure_code": "HISTORY_PUBLICATION_FAILED",
            },
        )
        raise HistoryUnavailable(
            "Historical build failed; last-good publication remains audit-only."
        ) from None


def read_history(
    cfg: Settings,
    *,
    calendar_publication_id: str,
    calendar_manifest_sha256: str,
    ticker: str | None = None,
    retained: bool = False,
) -> dict[str, Any]:
    empty = {
        "contract_version": CONTRACT,
        "history_state": "UNAVAILABLE",
        "is_current": False,
        "calendar_publication_id": calendar_publication_id,
        "data": None,
        "reason": "No validated actual-history publication is available.",
    }
    root = history_root(cfg)
    if not (root / "current.json").exists():
        return empty
    try:
        pointer = json.loads(bounded(root / "current.json", 10000))
        identifier = str(UUID(pointer["history_publication_id"]))
        directory = root / "publications" / identifier
        manifest_raw = bounded(directory / "manifest.json", 10000)
        manifest = json.loads(manifest_raw)
        raw = bounded(directory / "data.json", 12_000_000)
        pack = json.loads(raw)
        if (
            digest(manifest_raw) != pointer["manifest_sha256"]
            or digest(raw) != manifest["data_sha256"]
            or pack["history_publication_id"] != identifier
            or manifest["history_publication_id"] != identifier
            or pack["contract_version"] != CONTRACT
            or manifest["contract_version"] != CONTRACT
            or manifest["calendar_publication_id"] != pack["calendar_publication_id"]
            or manifest["market_publication_id"] != pack["market_publication_id"]
            or pack["method_version"] != METHOD
            or pack["scope_sha256"] != digest(encoded(pack["scope"]))
            or (cfg.dashboard_mode == "live" and pack["evidence_mode"] != "live")
        ):
            raise ValueError
        market = read_market(
            cfg,
            calendar_publication_id=calendar_publication_id,
            calendar_manifest_sha256=calendar_manifest_sha256,
            retained=True,
        )
        state = "HEALTHY"
        if (
            pack["calendar_publication_id"] != calendar_publication_id
            or pack["calendar_manifest_sha256"] != calendar_manifest_sha256
            or not market["data"]
            or pack["market_publication_id"] != market["data"]["market_publication_id"]
        ):
            return {
                **empty,
                "history_state": "SUPERSEDED",
                "reason": "Historical dependencies changed. Rebuild from accepted inputs.",
            }
        # Reproduce source assertions and arithmetic, rather than trusting a checksum alone.
        source = extract_history(cfg, pack["source_bundle_sha256"], pack["scope"])
        if any(
            pack[key] != source[key]
            for key in ("events", "exclusions", "primary_documents_checked")
        ):
            raise ValueError
        if pack["scope"] != market["data"]["scope"]:
            raise ValueError
        price_index = indexed_prices(market["data"])
        expected = [
            price_reaction(e, listing, market["data"], price_index)
            for e in pack["events"]
            for listing in pack["scope"]
            if e["cik"] == listing["cik"]
        ]
        if (
            pack["reactions"] != expected
            or pack["market_trust_deadline"] != market["data"]["trust_deadline"]
        ):
            raise ValueError
        coverage = [
            {
                "ticker": listing["ticker"],
                "actual_events": sum(e["cik"] == listing["cik"] for e in pack["events"]),
                "available_reactions": sum(
                    r["ticker"] == listing["ticker"] and r["state"] == "AVAILABLE" for r in expected
                ),
            }
            for listing in pack["scope"]
        ]
        if pack["coverage"] != coverage:
            raise ValueError
        if not market["is_current"]:
            state = market["market_state"]
        status = json.loads(bounded(root / "status.json", 10000))
        if status.get("state") != "SUCCEEDED" or status.get("history_publication_id") != identifier:
            state = "REFRESH_FAILED"
        if state != "HEALTHY" and not retained:
            return {
                **empty,
                "history_state": state,
                "reason": "Historical input trust is unavailable; audit data are separate.",
            }
        if ticker:
            name = ticker.upper()
            cik = next((s["cik"] for s in pack["scope"] if s["ticker"] == name), None)
            pack["events"] = [e for e in pack["events"] if e["cik"] == cik]
            pack["reactions"] = [r for r in pack["reactions"] if r["ticker"] == name]
            pack["coverage"] = [r for r in pack["coverage"] if r["ticker"] == name]
        return {
            **empty,
            "history_state": state,
            "is_current": state == "HEALTHY",
            "data": pack,
            "reason": None if state == "HEALTHY" else "Retained historical inputs; not current.",
        }
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        raise HistoryUnavailable(
            "Historical publication or source evidence failed integrity validation."
        ) from None
