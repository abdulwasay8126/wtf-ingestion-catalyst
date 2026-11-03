"""Bounded historical public data only; no trading or current-feed requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from wtf_ingestion.adapters.base import (
    AccessBlockedError,
    PayloadError,
    PayloadTooLargeError,
    RequestGate,
    SourcePermissionError,
    ThrottledError,
    TransportError,
)
from wtf_ingestion.config import Settings
from wtf_ingestion.safety import validate_public_url

ENDPOINTS = {
    "prices": "https://data.alpaca.markets/v2/stocks/bars",
    "actions": "https://data.alpaca.markets/v1/corporate-actions",
    "news": "https://data.alpaca.markets/v1beta1/news",
    "calendar": "https://paper-api.alpaca.markets/v2/calendar",
}
ACTION_TYPES = {
    "reverse_splits",
    "forward_splits",
    "unit_splits",
    "cash_dividends",
    "stock_dividends",
    "spin_offs",
    "cash_mergers",
    "stock_mergers",
    "stock_and_cash_mergers",
    "redemptions",
    "name_changes",
    "worthless_removals",
    "rights_distributions",
    "partial_calls",
    "reorganizations",
    "capital_gains_distributions",
}


def symbol(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value):
        raise PayloadError("Invalid public stock symbol.")
    return value


def instant(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        raise PayloadError("Source timestamp requires a valid timezone.") from None


def number(value: Any, *, positive: bool = True) -> str:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or (result <= 0 if positive else result < 0):
            raise ValueError
        return str(result)
    except (InvalidOperation, ValueError):
        raise PayloadError("Source numeric value is invalid.") from None


def parse_page(
    dataset: str, body: Any, requested: set[str]
) -> tuple[list[dict[str, Any]], str | None]:
    """Validate before normalization; empty and changed-layout responses differ."""
    rows: list[dict[str, Any]] = []
    try:
        if dataset == "calendar":
            if not isinstance(body, list):
                raise ValueError
            for item in body:
                day = date.fromisoformat(item["date"])
                opening, closing = (
                    time.fromisoformat(item["open"]),
                    time.fromisoformat(item["close"]),
                )
                if opening >= closing:
                    raise ValueError
                rows.append(
                    {
                        "date": day.isoformat(),
                        "open": opening.isoformat(),
                        "close": closing.isoformat(),
                        "timezone": "America/New_York",
                    }
                )
            return rows, None
        if not isinstance(body, dict):
            raise ValueError
        token = body.get("next_page_token")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 4096):
            raise ValueError
        if dataset == "prices":
            groups = body["bars"]
            if not isinstance(groups, dict):
                raise ValueError
            for ticker, bars in groups.items():
                if symbol(ticker) not in requested or not isinstance(bars, list):
                    raise ValueError
                for bar in bars:
                    stamp = instant(bar["t"])
                    prices = {
                        name: number(bar[key])
                        for name, key in (
                            ("open", "o"),
                            ("high", "h"),
                            ("low", "l"),
                            ("close", "c"),
                        )
                    }
                    low, high = Decimal(prices["low"]), Decimal(prices["high"])
                    if (
                        not low <= Decimal(prices["open"]) <= high
                        or not low <= Decimal(prices["close"]) <= high
                    ):
                        raise ValueError
                    volume = bar["v"]
                    trades = bar.get("n")
                    if (
                        type(volume) is not int
                        or volume < 0
                        or (trades is not None and (type(trades) is not int or trades < 0))
                    ):
                        raise ValueError
                    rows.append(
                        {
                            "ticker": ticker,
                            "timestamp": stamp.isoformat(),
                            "trading_date": stamp.astimezone(ZoneInfo("America/New_York"))
                            .date()
                            .isoformat(),
                            **prices,
                            "volume": volume,
                            "trade_count": trades,
                            "vwap": number(bar["vw"]) if bar.get("vw") is not None else None,
                            "currency": "USD",
                            "interval": "1Day",
                            "feed": "sip",
                            "adjustment": "all",
                        }
                    )
        elif dataset == "news":
            if not isinstance(body["news"], list):
                raise ValueError
            for item in body["news"]:
                if not isinstance(item["symbols"], list):
                    raise ValueError
                tickers = [symbol(t) for t in item["symbols"]]
                if not set(tickers) & requested:
                    continue
                created, updated = instant(item["created_at"]), instant(item["updated_at"])
                validate_public_url(item["url"])
                if (
                    updated < created
                    or not isinstance(item["headline"], str)
                    or not item["headline"].strip()
                    or len(item["headline"]) > 2000
                ):
                    raise ValueError
                if type(item["id"]) is not int or not isinstance(item["source"], str):
                    raise ValueError
                rows.append(
                    {
                        "id": str(item["id"]),
                        "headline": item["headline"],
                        "url": item["url"],
                        "publisher": item["source"],
                        "tickers": tickers,
                        "published_at": created.isoformat(),
                        "updated_at": updated.isoformat(),
                        "role": "discovery_only",
                    }
                )
        elif dataset == "actions":
            groups = body["corporate_actions"]
            if not isinstance(groups, dict) or set(groups) - ACTION_TYPES:
                raise ValueError
            for kind, items in groups.items():
                if not isinstance(items, list):
                    raise ValueError
                for item in items:
                    tickers = [
                        symbol(v)
                        for k, v in item.items()
                        if (k == "symbol" or k.endswith("_symbol")) and v is not None
                    ]
                    if not set(tickers) & requested or not isinstance(item["id"], str):
                        raise ValueError
                    date.fromisoformat(item["process_date"])
                    for key, value in item.items():
                        if value is not None and key.endswith("_date"):
                            date.fromisoformat(value)
                        if value is not None and (key == "rate" or key.endswith("_rate")):
                            number(value, positive=False)
                    rows.append(
                        {
                            "id": item["id"],
                            "type": kind,
                            "tickers": tickers,
                            "process_date": item["process_date"],
                            "source_fields": item,
                        }
                    )
        else:
            raise ValueError
        return rows, token
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PayloadError("Alpaca response schema or source fields failed validation.") from None


class AlpacaClient:
    def __init__(self, cfg: Settings, *, transport: httpx.BaseTransport | None = None):
        if transport is None:
            cfg.assert_live_alpaca_ready()
        elif cfg.dashboard_mode != "fixture":
            raise SourcePermissionError("Fixture transport is forbidden in live mode.")
        self.mode = "fixture" if transport is not None else "live"
        self.cfg = cfg
        self.gate = RequestGate(1.0 if transport is None else 0.0)
        self.client = httpx.Client(
            transport=transport, follow_redirects=False, timeout=cfg.request_timeout_seconds
        )
        if transport is None:
            assert cfg.alpaca_api_key and cfg.alpaca_api_secret
            self.client.headers.update(
                {
                    "APCA-API-KEY-ID": cfg.alpaca_api_key.get_secret_value(),
                    "APCA-API-SECRET-KEY": cfg.alpaca_api_secret.get_secret_value(),
                }
            )
        self.pages = 0

    def close(self) -> None:
        self.client.headers.clear()
        self.client.close()

    def pages_for(
        self,
        dataset: str,
        params: dict[str, str],
        requested: set[str],
        capture: Callable[[bytes, dict[str, Any]], str],
    ) -> tuple[list[dict[str, Any]], bool]:
        if dataset not in ENDPOINTS:
            raise SourcePermissionError("Endpoint is outside the public market-data allowlist.")
        if dataset == "prices" and any(
            params.get(k) != v
            for k, v in {
                "feed": "sip",
                "timeframe": "1Day",
                "adjustment": "all",
                "currency": "USD",
            }.items()
        ):
            raise SourcePermissionError("Price requests require the fixed historical policy.")
        if dataset == "news" and params.get("include_content") != "false":
            raise SourcePermissionError("Article content collection is excluded.")
        records: dict[tuple[str, str], dict[str, Any]] = {}
        tokens: set[str] = set()
        query = dict(params)
        while True:
            self.pages += 1
            if self.pages > self.cfg.alpaca_max_pages:
                raise PayloadError("Alpaca pagination exceeded the global page bound.")
            self.gate.acquire()
            url = ENDPOINTS[dataset] + "?" + urlencode(query)
            try:
                with self.client.stream("GET", url) as response:
                    status = response.status_code
                    if status in (401, 403, 429):
                        self.gate.stop()
                        if status == 429:
                            raise ThrottledError("Alpaca access stopped after rate refusal.")
                        raise AccessBlockedError(
                            "Alpaca access stopped after authorization refusal."
                        )
                    if 300 <= status < 400:
                        raise SourcePermissionError("Alpaca redirects are forbidden.")
                    if status != 200:
                        raise TransportError("Alpaca returned an unsuccessful status.")
                    if "application/json" not in response.headers.get("content-type", "").lower():
                        raise PayloadError("Alpaca returned an unexpected content type.")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > min(self.cfg.max_response_bytes, 2_000_000):
                            raise PayloadTooLargeError("Alpaca response exceeded its byte bound.")
                    payload = bytes(raw)
            except httpx.HTTPError:
                raise TransportError("Alpaca network request failed.") from None
            for value in (self.cfg.alpaca_api_key, self.cfg.alpaca_api_secret):
                if value and value.get_secret_value().encode() in payload:
                    raise PayloadError("Response failed sensitive-value exclusion.")
            observed = datetime.now(UTC)
            source_hash = hashlib.sha256(payload).hexdigest()
            capture_kind = "raw_response"
            if dataset == "news":
                # News responses also carry bylines, summaries and media. Retain only
                # the agreed public metadata, and the hash of the ephemeral response.
                try:
                    original = json.loads(payload)
                    allowed = {
                        "id",
                        "headline",
                        "url",
                        "source",
                        "symbols",
                        "created_at",
                        "updated_at",
                    }
                    projected = {
                        k: v for k, v in original.items() if k in {"news", "next_page_token"}
                    }
                    if isinstance(projected.get("news"), list):
                        projected["news"] = [
                            {k: v for k, v in item.items() if k in allowed}
                            if isinstance(item, dict)
                            else item
                            for item in projected["news"]
                        ]
                    payload = json.dumps(projected, allow_nan=False).encode()
                    capture_kind = "news_metadata_projection"
                except (ValueError, TypeError, AttributeError):
                    raise PayloadError("News metadata projection failed validation.") from None
            source_url = (
                ENDPOINTS[dataset]
                + "?"
                + urlencode({k: v for k, v in query.items() if k != "page_token"})
            )
            reference = capture(
                payload,
                {
                    "dataset": dataset,
                    "source_url": source_url,
                    "observed_at": observed.isoformat(),
                    "page": self.pages,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "source_response_sha256": source_hash,
                    "capture_kind": capture_kind,
                },
            )
            try:
                body = json.loads(
                    payload,
                    parse_float=Decimal if dataset == "prices" else float,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                )
            except (ValueError, UnicodeError):
                raise PayloadError("Alpaca response is not valid JSON.") from None
            rows, token = parse_page(dataset, body, requested)
            for row in rows:
                if dataset == "prices":
                    key = (row["ticker"], row["timestamp"])
                    if instant(row["timestamp"]) > observed:
                        raise PayloadError("Future price observation is invalid.")
                    if (
                        not instant(params["start"])
                        <= instant(row["timestamp"])
                        <= instant(params["end"])
                    ):
                        raise PayloadError("Price observation is outside the requested interval.")
                elif dataset == "news":
                    key = (dataset, row["id"])
                    if instant(row["updated_at"]) > observed:
                        raise PayloadError("Future news update timestamp is invalid.")
                elif dataset == "actions":
                    key = (dataset, row["id"])
                    if not params["start"] <= row["process_date"] <= params["end"]:
                        raise PayloadError("Corporate action is outside the process-date interval.")
                else:
                    key = (dataset, row.get("id", row.get("date", "")))
                fact = dict(row)
                if (
                    key in records
                    and {k: v for k, v in records[key].items() if k != "evidence_id"} != fact
                ):
                    raise PayloadError("Conflicting duplicate source observation.")
                records[key] = {**fact, "evidence_id": reference}
            if dataset == "news" or not token:
                return list(records.values()), bool(token)
            if token in tokens:
                raise PayloadError("Alpaca pagination cycle detected.")
            tokens.add(token)
            query["page_token"] = token
