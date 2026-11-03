"""Immutable market publications with independent trust and retained raw proof."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from wtf_ingestion.adapters.alpaca import ENDPOINTS, AlpacaClient, instant, parse_page, symbol
from wtf_ingestion.adapters.base import (
    AccessBlockedError,
    PayloadError,
    SourceError,
    ThrottledError,
)
from wtf_ingestion.config import Settings

CONTRACT = "earnings-market-0.1.0"
MAX_PACK = 60_000_000
_verified_market: OrderedDict[str, None] = OrderedDict()
_verification_lock = Lock()
_market_read_lock = Lock()


class MarketUnavailable(RuntimeError):
    pass


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def market_root(cfg: Settings) -> Path:
    return cfg.output_dir.resolve().parent / "market"


def atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("xb") as handle:
        handle.write(encoded(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def bounded(path: Path, limit: int = MAX_PACK) -> bytes:
    if path.stat().st_size > limit:
        raise MarketUnavailable("Market evidence exceeded its byte bound.")
    return path.read_bytes()


def scope_from_feed(feed: dict[str, Any]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for issuer in [*feed["data"], *feed["missing_issuers"]]:
        cik = issuer.get("cik") or issuer.get("event", {}).get("cik")
        for listing in issuer["listings"]:
            ticker = symbol(listing["ticker"])
            row = {
                "ticker": ticker,
                "cik": cik,
                "company_id": issuer["company_id"],
                "exchange": listing.get("exchange"),
            }
            if ticker in result and result[ticker] != row:
                raise PayloadError("Conflicting issuer/listing scope.")
            result[ticker] = row
    if not result:
        raise PayloadError("No fixed listing scope is available.")
    return sorted(result.values(), key=lambda item: item["ticker"])


def collect_market(
    cfg: Settings,
    client: AlpacaClient,
    *,
    calendar_publication_id: str,
    calendar_manifest_sha256: str,
    scope: list[dict[str, Any]],
) -> dict[str, Any]:
    """Caller holds the pipeline lease; no SEC or event assertion is modified."""
    root = market_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    status_path = root / "status.json"
    if status_path.exists():
        try:
            if json.loads(status_path.read_bytes()).get("access_disabled"):
                client.close()
                raise MarketUnavailable(
                    "Alpaca is disabled after access refusal; operator review required."
                )
        except (OSError, ValueError):
            client.close()
            raise MarketUnavailable("Market source status failed validation.") from None
    if cfg.dashboard_mode == "live" and client.mode != "live":
        client.close()
        raise MarketUnavailable("Live market publication refuses fixture evidence.")
    identifier = str(uuid4())
    attempt = root / "attempts" / identifier
    attempt.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC)
    status = {
        "attempt_id": identifier,
        "state": "COLLECTING",
        "access_disabled": False,
        "started_at": started.isoformat(),
        "evidence_mode": client.mode,
    }
    atomic(status_path, status)
    captures: list[dict[str, Any]] = []
    raw_root = (cfg.raw_dir / "alpaca").resolve()
    raw_root.mkdir(parents=True, exist_ok=True)

    def capture(raw: bytes, meta: dict[str, Any]) -> str:
        name = digest(raw) + ".json"
        path = raw_root / name
        if path.exists():
            if digest(bounded(path, 2_000_000)) != meta["sha256"]:
                raise PayloadError("Retained market capture checksum failed.")
        else:
            with path.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        evidence_id = str(uuid4())
        captures.append({**meta, "id": evidence_id, "raw_file": name, "evidence_mode": client.mode})
        atomic(attempt / "captures.json", captures)
        return evidence_id

    try:
        tickers = [symbol(item["ticker"]) for item in scope]
        if len(tickers) != len(set(tickers)) or len(tickers) > 1000:
            raise PayloadError("Market listing scope is duplicate or excessive.")
        zone = ZoneInfo("America/New_York")
        midnight = datetime.combine(started.astimezone(zone).date(), datetime.min.time(), zone)
        end = min(
            started - timedelta(minutes=20), midnight.astimezone(UTC) - timedelta(microseconds=1)
        )
        start = datetime.combine(
            end.astimezone(zone).date() - timedelta(days=cfg.alpaca_history_days),
            datetime.min.time(),
            zone,
        ).astimezone(UTC)
        date_params = {
            "start": start.astimezone(zone).date().isoformat(),
            "end": end.astimezone(zone).date().isoformat(),
        }
        calendar, _ = client.pages_for("calendar", date_params, set(tickers), capture)
        if not calendar:
            raise PayloadError("Trading calendar is suspiciously empty for the requested week(s).")
        if any(not date_params["start"] <= item["date"] <= date_params["end"] for item in calendar):
            raise PayloadError("Trading calendar is outside the requested interval.")
        prices: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        news: dict[str, dict[str, Any]] = {}
        sampled_news = False
        for offset in range(0, len(tickers), 100):
            batch = tickers[offset : offset + 100]
            requested = set(batch)
            common = {"symbols": ",".join(batch)}
            bars, _ = client.pages_for(
                "prices",
                {
                    **common,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "timeframe": "1Day",
                    "feed": "sip",
                    "adjustment": "all",
                    "asof": end.astimezone(zone).date().isoformat(),
                    "currency": "USD",
                    "limit": "10000",
                },
                requested,
                capture,
            )
            prices.extend(bars)
            events, _ = client.pages_for(
                "actions",
                {**common, **date_params, "data_quality": "complete", "limit": "1000"},
                requested,
                capture,
            )
            actions.extend(events)
            articles, has_more = client.pages_for(
                "news",
                {
                    **common,
                    "start": (end - timedelta(days=7)).isoformat(),
                    "end": end.isoformat(),
                    "limit": "50",
                    "sort": "desc",
                    "include_content": "false",
                },
                requested,
                capture,
            )
            sampled_news |= has_more
            for article in articles:
                previous = news.get(article["id"])
                if previous and {k: v for k, v in previous.items() if k != "evidence_id"} != {
                    k: v for k, v in article.items() if k != "evidence_id"
                }:
                    raise PayloadError("News revision conflict during collection.")
                news[article["id"]] = article
        if not prices:
            raise PayloadError("Prices are suspiciously empty across the whole requested scope.")
        trading_days = {item["date"] for item in calendar}
        if any(bar["trading_date"] not in trading_days for bar in prices):
            raise PayloadError("Daily price is outside the returned trading calendar.")
        identities = {item["ticker"]: item for item in scope}
        for bar in prices:
            bar["listing"] = identities[bar["ticker"]]
            bar["identity_basis"] = "current_listing_provider_asof_mapping_unverified"
        observed = [instant(item["observed_at"]) for item in captures]
        pack = {
            "contract_version": CONTRACT,
            "market_publication_id": identifier,
            "calendar_publication_id": calendar_publication_id,
            "calendar_manifest_sha256": calendar_manifest_sha256,
            "evidence_mode": client.mode,
            "provider": "alpaca",
            "observed_from": min(observed).isoformat(),
            "observed_through": max(observed).isoformat(),
            "trust_deadline": (
                min(observed) + timedelta(hours=cfg.alpaca_freshness_hours)
            ).isoformat(),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "freshness_hours": cfg.alpaca_freshness_hours,
            "scope": scope,
            "scope_sha256": digest(encoded(scope)),
            "prices": sorted(prices, key=lambda item: (item["ticker"], item["timestamp"])),
            "corporate_actions": actions,
            "news": list(news.values()),
            "trading_calendar": calendar,
            "news_scope": "Latest 50 per 100-symbol batch over seven days; discovery only.",
            "news_has_more": sampled_news,
            "evidence": captures,
            "coverage": [
                {
                    "ticker": ticker,
                    "observed_days": len(
                        {b["trading_date"] for b in prices if b["ticker"] == ticker}
                    ),
                    "missing_trading_dates": sorted(
                        trading_days - {b["trading_date"] for b in prices if b["ticker"] == ticker}
                    ),
                }
                for ticker in tickers
            ],
            "rights_reference": cfg.alpaca_rights_reference,
            "rights_datasets": list(cfg.alpaca_rights_datasets),
            "actual_earnings_history_included": False,
            "reactions_calculated": False,
        }
        body = encoded(pack)
        if len(body) > MAX_PACK:
            raise PayloadError("Market publication exceeds the pack byte bound.")
        staging = root / "publications" / (identifier + ".staging")
        staging.mkdir(parents=True, exist_ok=False)
        with (staging / "data.json").open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        manifest = {
            "market_publication_id": identifier,
            "contract_version": CONTRACT,
            "data_sha256": digest(body),
            "calendar_publication_id": calendar_publication_id,
            "calendar_manifest_sha256": calendar_manifest_sha256,
            "evidence_mode": client.mode,
        }
        atomic(staging / "manifest.json", manifest)
        directory = staging.with_name(identifier)
        staging.rename(directory)
        status = {
            **status,
            "state": "SUCCEEDED",
            "finished_at": datetime.now(UTC).isoformat(),
            "market_publication_id": identifier,
        }
        atomic(attempt / "result.json", status)
        atomic(status_path, status)
        atomic(
            root / "current.json",
            {"market_publication_id": identifier, "manifest_sha256": digest(encoded(manifest))},
        )
        return {
            "market_publication_id": identifier,
            "state": "SUCCEEDED",
            "price_rows": len(prices),
            "listings_with_prices": len({b["ticker"] for b in prices}),
            "requested_listings": len(scope),
            "news_rows": len(news),
            "action_rows": len(actions),
        }
    except (SourceError, MarketUnavailable, OSError, ValueError) as exc:
        status = {
            **status,
            "state": "FAILED",
            "finished_at": datetime.now(UTC).isoformat(),
            "access_disabled": isinstance(exc, (AccessBlockedError, ThrottledError)),
            "failure_code": exc.code
            if isinstance(exc, SourceError)
            else "MARKET_PUBLICATION_FAILED",
        }
        atomic(attempt / "result.json", status)
        atomic(status_path, status)
        raise MarketUnavailable(
            "Market refresh failed; last-good evidence is retained for audit."
        ) from None
    finally:
        client.close()


def read_market(
    cfg: Settings,
    *,
    calendar_publication_id: str,
    calendar_manifest_sha256: str,
    ticker: str | None = None,
    retained: bool = False,
) -> dict[str, Any]:
    # One bounded validation at a time prevents duplicate large parse allocations
    # when the market and history panels request the same publication together.
    with _market_read_lock:
        return _read_market(
            cfg,
            calendar_publication_id=calendar_publication_id,
            calendar_manifest_sha256=calendar_manifest_sha256,
            ticker=ticker,
            retained=retained,
        )


def _read_market(
    cfg: Settings,
    *,
    calendar_publication_id: str,
    calendar_manifest_sha256: str,
    ticker: str | None = None,
    retained: bool = False,
) -> dict[str, Any]:
    root = market_root(cfg)
    empty = {
        "contract_version": CONTRACT,
        "market_state": "UNAVAILABLE",
        "is_current": False,
        "calendar_publication_id": calendar_publication_id,
        "data": None,
        "reason": "No validated market publication is available.",
    }
    if not (root / "current.json").exists():
        if (root / "status.json").exists():
            try:
                status = json.loads(bounded(root / "status.json", 10000))
                empty["market_state"] = (
                    "ACCESS_BLOCKED" if status.get("access_disabled") else "UNAVAILABLE"
                )
            except (OSError, ValueError, TypeError):
                raise MarketUnavailable("Market source status failed validation.") from None
        return empty
    try:
        pointer = json.loads(bounded(root / "current.json", 10000))
        identifier = str(UUID(pointer["market_publication_id"]))
        directory = (root / "publications" / identifier).resolve()
        if directory.parent != (root / "publications").resolve():
            raise ValueError
        manifest_raw = bounded(directory / "manifest.json", 10000)
        if digest(manifest_raw) != pointer["manifest_sha256"]:
            raise ValueError
        manifest = json.loads(manifest_raw)
        raw = bounded(directory / "data.json")
        if digest(raw) != manifest["data_sha256"]:
            raise ValueError
        pack = json.loads(raw)
        with _verification_lock:
            previously_verified = manifest["data_sha256"] in _verified_market
        if (
            pack["contract_version"] != CONTRACT
            or manifest["contract_version"] != CONTRACT
            or pack["market_publication_id"] != identifier
            or manifest["market_publication_id"] != identifier
            or pack["calendar_publication_id"] != manifest["calendar_publication_id"]
            or pack["calendar_manifest_sha256"] != manifest["calendar_manifest_sha256"]
            or pack["evidence_mode"] != manifest["evidence_mode"]
            or pack["scope_sha256"] != digest(encoded(pack["scope"]))
        ):
            raise ValueError
        if cfg.dashboard_mode == "live" and pack["evidence_mode"] != "live":
            raise MarketUnavailable("Live market reads refuse fixture evidence.")
        if pack["evidence_mode"] == "live" and (
            not pack["rights_reference"]
            or set(pack["rights_datasets"]) != {"prices", "actions", "news", "calendar"}
        ):
            raise MarketUnavailable("Published market data lacks the rights record.")
        evidence = {item["id"]: item for item in pack["evidence"]}
        raw_root = (cfg.raw_dir / "alpaca").resolve()
        proof: dict[str, set[bytes]] = {}
        scope = {item["ticker"]: item for item in pack["scope"]}
        for item in evidence.values():
            filename = item["sha256"] + ".json"
            path = (raw_root / filename).resolve()
            if item["raw_file"] != filename or path.parent != raw_root:
                raise ValueError
            captured_raw = bounded(path, 2_000_000)
            if digest(captured_raw) != item["sha256"]:
                raise ValueError
            if item["evidence_mode"] != pack["evidence_mode"]:
                raise ValueError
            endpoint, _, query = item["source_url"].partition("?")
            if endpoint != ENDPOINTS[item["dataset"]]:
                raise ValueError
            requested = set(parse_qs(query).get("symbols", [","])[0].split(",")) - {""}
            if requested - scope.keys():
                raise ValueError
            parameters = parse_qs(query)
            if item["dataset"] == "prices" and any(
                parameters.get(k) != [v]
                for k, v in {
                    "feed": "sip",
                    "timeframe": "1Day",
                    "adjustment": "all",
                    "currency": "USD",
                }.items()
            ):
                raise ValueError
            if previously_verified:
                continue
            rows, _ = parse_page(
                item["dataset"],
                json.loads(
                    captured_raw,
                    parse_float=Decimal if item["dataset"] == "prices" else float,
                ),
                requested,
            )
            proof[item["id"]] = {encoded(row) for row in rows}
        for key, dataset in (
            ("prices", "prices"),
            ("corporate_actions", "actions"),
            ("news", "news"),
            ("trading_calendar", "calendar"),
        ):
            for row in [] if previously_verified else pack[key]:
                fact = {
                    k: v
                    for k, v in row.items()
                    if k not in ("evidence_id", "listing", "identity_basis")
                }
                if (
                    row["evidence_id"] not in evidence
                    or evidence[row["evidence_id"]]["dataset"] != dataset
                    or encoded(fact) not in proof[row["evidence_id"]]
                ):
                    raise ValueError
                if key == "prices" and (
                    row["listing"] != scope[row["ticker"]]
                    or row["identity_basis"] != "current_listing_provider_asof_mapping_unverified"
                    or not instant(pack["window"]["start"])
                    <= instant(row["timestamp"])
                    <= instant(pack["window"]["end"])
                    or instant(row["timestamp"])
                    > instant(evidence[row["evidence_id"]]["observed_at"])
                ):
                    raise ValueError
        days = {item["date"] for item in pack["trading_calendar"]}
        if (
            not days
            or not pack["prices"]
            or any(bar["trading_date"] not in days for bar in pack["prices"])
        ):
            raise ValueError
        observed_days: dict[str, set[str]] = {name: set() for name in scope}
        for bar in pack["prices"]:
            observed_days[bar["ticker"]].add(bar["trading_date"])
        expected_coverage = [
            {
                "ticker": name,
                "observed_days": len(observed_days[name]),
                "missing_trading_dates": sorted(days - observed_days[name]),
            }
            for name in scope
        ]
        if pack["coverage"] != expected_coverage:
            raise ValueError
        # Reuse exact immutable validation only. Every read still hashes the pack and
        # every capture, recomputes coverage and checks current status/clocks below.
        with _verification_lock:
            _verified_market[manifest["data_sha256"]] = None
            if len(_verified_market) > 8:
                _verified_market.popitem(last=False)
        clocks = [instant(item["observed_at"]) for item in evidence.values()]
        if (
            instant(pack["observed_from"]) != min(clocks)
            or instant(pack["observed_through"]) != max(clocks)
            or max(clocks) > datetime.now(UTC)
            or not 1 <= pack["freshness_hours"] <= 168
            or instant(pack["trust_deadline"])
            != min(clocks) + timedelta(hours=pack["freshness_hours"])
        ):
            raise ValueError
        state = "HEALTHY"
        if (
            pack["calendar_publication_id"] != calendar_publication_id
            or pack["calendar_manifest_sha256"] != calendar_manifest_sha256
        ):
            state = "SUPERSEDED"
        if instant(pack["trust_deadline"]) < datetime.now(UTC):
            state = "STALE"
        status = json.loads(bounded(root / "status.json", 10000))
        if status["state"] != "SUCCEEDED" or status.get("market_publication_id") != identifier:
            state = "ACCESS_BLOCKED" if status.get("access_disabled") else "REFRESH_FAILED"
        if state != "HEALTHY" and not retained:
            return {
                **empty,
                "market_state": state,
                "reason": "Current market trust is unavailable; retained audit data is separate.",
            }
        if ticker:
            name = symbol(ticker.upper())
            pack["scope"] = [item for item in pack["scope"] if item["ticker"] == name]
            pack["coverage"] = [item for item in pack["coverage"] if item["ticker"] == name]
            pack["prices"] = [item for item in pack["prices"] if item["ticker"] == name]
            for key in ("news", "corporate_actions"):
                pack[key] = [item for item in pack[key] if name in item["tickers"]]
        return {
            "contract_version": CONTRACT,
            "market_state": state,
            "is_current": state == "HEALTHY",
            "calendar_publication_id": calendar_publication_id,
            "data": pack,
            "reason": None if state == "HEALTHY" else "Retained market evidence; not current.",
        }
    except MarketUnavailable:
        raise
    except (OSError, ValueError, TypeError, KeyError, SourceError):
        raise MarketUnavailable(
            "Market publication or retained evidence failed integrity validation."
        ) from None
