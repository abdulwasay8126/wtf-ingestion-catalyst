from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from freezegun import freeze_time
from pydantic import SecretStr

from wtf_ingestion.adapters.alpaca import AlpacaClient, instant, number, parse_page
from wtf_ingestion.adapters.base import (
    AccessBlockedError,
    PayloadError,
    PayloadTooLargeError,
    SourcePermissionError,
    ThrottledError,
)
from wtf_ingestion.config import Settings
from wtf_ingestion.market_data import (
    MarketUnavailable,
    collect_market,
    digest,
    encoded,
    market_root,
    read_market,
    scope_from_feed,
)

BAR = {
    "t": "2024-07-31T04:00:00Z",
    "o": 100,
    "h": 110,
    "l": 90,
    "c": 105,
    "v": 10,
    "n": 2,
    "vw": 102.5,
}
PARAMS = {
    "start": "2024-07-01T00:00:00Z",
    "end": "2024-08-01T00:00:00Z",
    "feed": "sip",
    "timeframe": "1Day",
    "adjustment": "all",
    "currency": "USD",
}
SCOPE = [
    {"ticker": t, "cik": str(i), "company_id": str(uuid4()), "exchange": "NASDAQ"}
    for i, t in enumerate(["AAPL", "BRK.B"], 1)
]
CAL = str(uuid4())
CAL_HASH = "a" * 64


def cfg_for(tmp_path, **kwargs):  # type: ignore[no-untyped-def]
    return Settings(
        _env_file=None, output_dir=tmp_path / "publications", raw_dir=tmp_path / "raw", **kwargs
    )


def response_for(request):  # type: ignore[no-untyped-def]
    assert request.method == "GET"
    assert "APCA-API-KEY-ID" not in request.headers
    if request.url.path == "/v2/calendar":
        return httpx.Response(200, json=[{"date": "2024-07-31", "open": "09:30", "close": "16:00"}])
    if request.url.path == "/v2/stocks/bars":
        assert request.url.params["feed"] == "sip"
        assert request.url.params["adjustment"] == "all"
        return httpx.Response(200, json={"bars": {"AAPL": [BAR]}, "next_page_token": None})
    if request.url.path == "/v1/corporate-actions":
        return httpx.Response(
            200,
            json={
                "corporate_actions": {
                    "cash_dividends": [
                        {
                            "id": "action-1",
                            "symbol": "AAPL",
                            "process_date": "2024-07-31",
                            "ex_date": "2024-08-09",
                            "rate": "0.25",
                        }
                    ]
                },
                "next_page_token": None,
            },
        )
    assert request.url.path == "/v1beta1/news"
    assert request.url.params["include_content"] == "false"
    return httpx.Response(
        200,
        json={
            "news": [
                {
                    "id": 1,
                    "headline": "A <source> headline",
                    "url": "https://www.example.com/news/1",
                    "source": "Public publisher",
                    "symbols": ["AAPL"],
                    "created_at": "2024-07-30T12:00:00Z",
                    "updated_at": "2024-07-30T13:00:00Z",
                    "author": "Excluded byline",
                    "content": "Excluded article",
                    "summary": "Excluded summary",
                }
            ],
            "next_page_token": "bounded-news-sample",
        },
    )


def publish(cfg, handler=response_for):  # type: ignore[no-untyped-def]
    return collect_market(
        cfg,
        AlpacaClient(cfg, transport=httpx.MockTransport(handler)),
        calendar_publication_id=CAL,
        calendar_manifest_sha256=CAL_HASH,
        scope=SCOPE,
    )


def read(cfg, **kwargs):  # type: ignore[no-untyped-def]
    return read_market(
        cfg, calendar_publication_id=CAL, calendar_manifest_sha256=CAL_HASH, **kwargs
    )


@pytest.mark.parametrize(
    "dataset,body",
    [
        ("prices", {"bars": {}}),
        ("actions", {"corporate_actions": {}}),
        ("news", {"news": []}),
        ("calendar", []),
    ],
)
def test_legitimate_empty_source_pages(dataset, body):  # type: ignore[no-untyped-def]
    assert parse_page(dataset, body, {"AAPL"}) == ([], None)


@pytest.mark.parametrize(
    "dataset,body",
    [
        ("prices", {}),
        ("prices", {"bars": []}),
        ("prices", {"bars": {"UNKNOWN": [BAR]}}),
        ("prices", {"bars": {"AAPL": [{**BAR, "v": True}]}}),
        ("prices", {"bars": {"AAPL": [{**BAR, "o": 999}]}}),
        ("prices", {"bars": {"AAPL": [{**BAR, "c": "NaN"}]}}),
        ("actions", {"corporate_actions": {"new_schema": []}}),
        ("news", {"news": "partial"}),
        ("calendar", [{"date": "2024-07-31", "open": "16:00", "close": "09:30"}]),
    ],
)
def test_changed_or_invalid_schema_rejected(dataset, body):  # type: ignore[no-untyped-def]
    with pytest.raises(PayloadError):
        parse_page(dataset, body, {"AAPL"})


def test_precision_and_timezone_validation():
    assert number("0.000000123456789") == "1.23456789E-7"
    assert instant("2024-11-03T01:30:00-04:00") == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
    with pytest.raises(PayloadError):
        instant("2024-07-31T00:00:00")
    with pytest.raises(PayloadError):
        number(True)


@pytest.mark.parametrize(
    "status,error", [(401, AccessBlockedError), (403, AccessBlockedError), (429, ThrottledError)]
)
def test_refusal_stops_all_endpoints_without_retry(tmp_path, status, error):  # type: ignore[no-untyped-def]
    requests = []
    client = AlpacaClient(
        cfg_for(tmp_path),
        transport=httpx.MockTransport(
            lambda request: (requests.append(request), httpx.Response(status))[1]
        ),
    )
    with pytest.raises(error):
        client.pages_for("prices", PARAMS, {"AAPL"}, lambda *_: "ref")
    with pytest.raises(AccessBlockedError):
        client.pages_for("calendar", {}, {"AAPL"}, lambda *_: "ref")
    assert len(requests) == 1
    client.close()


@freeze_time("2024-08-01T12:00:00Z")
def test_pagination_duplicates_and_source_revisions(tmp_path):  # type: ignore[no-untyped-def]
    def handler(request):  # type: ignore[no-untyped-def]
        page = request.url.params.get("page_token")
        return httpx.Response(
            200, json={"bars": {"AAPL": [BAR]}, "next_page_token": None if page else "next"}
        )

    captures = []
    client = AlpacaClient(cfg_for(tmp_path), transport=httpx.MockTransport(handler))
    rows, more = client.pages_for(
        "prices",
        PARAMS,
        {"AAPL"},
        lambda raw, meta: (captures.append((raw, meta)), str(len(captures)))[1],
    )
    assert len(rows) == 1 and not more and len(captures) == 2
    assert all("page_token" not in meta["source_url"] for _, meta in captures)
    client.close()
    for changed in (False, True):

        def conflict(request, changed=changed):  # type: ignore[no-untyped-def]
            page = request.url.params.get("page_token")
            return httpx.Response(
                200,
                json={
                    "bars": {"AAPL": [{**BAR, "c": 106 if page and changed else 105}]},
                    "next_page_token": "cycle",
                },
            )

        client = AlpacaClient(cfg_for(tmp_path), transport=httpx.MockTransport(conflict))
        with pytest.raises(PayloadError, match=r"Conflicting|cycle"):
            client.pages_for("prices", PARAMS, {"AAPL"}, lambda *_: "ref")
        client.close()


def test_bounds_redirects_and_capture_before_validation(tmp_path):  # type: ignore[no-untyped-def]
    for response, error in [
        (
            httpx.Response(302, headers={"location": "https://elsewhere.example"}),
            SourcePermissionError,
        ),
        (
            httpx.Response(200, content=b"x" * 1100, headers={"content-type": "application/json"}),
            PayloadTooLargeError,
        ),
    ]:
        client = AlpacaClient(
            cfg_for(tmp_path, max_response_bytes=1000),
            transport=httpx.MockTransport(lambda _, response=response: response),
        )
        with pytest.raises(error):
            client.pages_for("prices", PARAMS, {"AAPL"}, lambda *_: "ref")
        client.close()
    captures = []
    client = AlpacaClient(
        cfg_for(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"partial": True})),
    )
    with pytest.raises(PayloadError):
        client.pages_for(
            "prices", PARAMS, {"AAPL"}, lambda raw, meta: (captures.append(raw), "ref")[1]
        )
    assert json.loads(captures[0]) == {"partial": True}
    client.close()


def test_secrets_disabled_and_live_fixture_boundary(tmp_path):  # type: ignore[no-untyped-def]
    cfg = cfg_for(
        tmp_path,
        alpaca_api_key=SecretStr("test-runtime-key"),
        alpaca_api_secret=SecretStr("test-runtime-secret"),
    )
    assert "test-runtime" not in repr(cfg) + cfg.model_dump_json()
    with pytest.raises(RuntimeError, match="disabled"):
        AlpacaClient(cfg)
    with pytest.raises(SourcePermissionError):
        AlpacaClient(
            cfg.model_copy(update={"dashboard_mode": "live"}),
            transport=httpx.MockTransport(response_for),
        )
    captures = []
    client = AlpacaClient(
        cfg,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"news": [], "echo": "test-runtime-secret"})
        ),
    )
    with pytest.raises(PayloadError, match="sensitive"):
        client.pages_for(
            "news",
            {**PARAMS, "include_content": "false"},
            {"AAPL"},
            lambda *args: captures.append(args),
        )
    assert not captures
    client.close()


@freeze_time("2024-08-01T12:00:00Z")
def test_atomic_publication_source_reproduction_and_missing_listing(tmp_path):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path)
    result = publish(cfg)
    payload = read(cfg)
    assert payload["is_current"]
    pack = payload["data"]
    assert result["price_rows"] == 1 and result["requested_listings"] == 2
    assert pack["coverage"][1]["missing_trading_dates"] == ["2024-07-31"]
    assert pack["news_has_more"] is True and pack["reactions_calculated"] is False
    assert (
        pack["corporate_actions"][0]["process_date"]
        != pack["corporate_actions"][0]["source_fields"]["ex_date"]
    )
    assert read(cfg, ticker="BRK.B")["data"]["prices"] == []
    news_capture = next(item for item in pack["evidence"] if item["dataset"] == "news")
    captured = (cfg.raw_dir / "alpaca" / news_capture["raw_file"]).read_text()
    assert "Excluded" not in captured and news_capture["capture_kind"] == "news_metadata_projection"
    old = (
        market_root(cfg) / "publications" / result["market_publication_id"] / "data.json"
    ).read_bytes()
    assert publish(cfg)["market_publication_id"] != result["market_publication_id"]
    assert (
        old
        == (
            market_root(cfg) / "publications" / result["market_publication_id"] / "data.json"
        ).read_bytes()
    )


@freeze_time("2024-08-01T12:00:00Z")
def test_failed_attempt_keeps_last_good_but_never_current(tmp_path):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path)
    publish(cfg)
    pointer = (market_root(cfg) / "current.json").read_bytes()
    with pytest.raises(MarketUnavailable):
        publish(cfg, lambda _: httpx.Response(200, json={"broken": "schema"}))
    assert (market_root(cfg) / "current.json").read_bytes() == pointer
    assert read(cfg)["market_state"] == "REFRESH_FAILED"
    assert read(cfg)["data"] is None
    assert read(cfg, retained=True)["data"]["prices"]
    assert list((market_root(cfg) / "attempts").glob("*/captures.json"))
    with pytest.raises(MarketUnavailable):
        publish(cfg, lambda _: httpx.Response(403))
    assert read(cfg)["market_state"] == "ACCESS_BLOCKED"
    calls = []
    with pytest.raises(MarketUnavailable, match="disabled"):
        publish(cfg, lambda request: (calls.append(request), response_for(request))[1])
    assert not calls


@freeze_time("2024-08-01T12:00:00Z")
def test_staleness_binding_and_fixture_rejection_even_retained(tmp_path):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path)
    publish(cfg)
    other = read_market(
        cfg, calendar_publication_id=str(uuid4()), calendar_manifest_sha256=CAL_HASH
    )
    assert other["market_state"] == "SUPERSEDED" and other["data"] is None
    with freeze_time("2024-08-03T12:00:00Z"):
        assert read(cfg)["market_state"] == "STALE"
        assert read(cfg, retained=True)["is_current"] is False
    with pytest.raises(MarketUnavailable, match="fixture"):
        read(cfg.model_copy(update={"dashboard_mode": "live"}), retained=True)


@freeze_time("2024-08-01T12:00:00Z")
@pytest.mark.parametrize("mutation", ["source", "fact", "coverage"])
def test_tamper_rejected_even_when_pack_hashes_rewritten(tmp_path, mutation):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path)
    result = publish(cfg)
    assert read(cfg)["is_current"]  # Warm exact-validation reuse before tampering.
    root = market_root(cfg)
    folder = root / "publications" / result["market_publication_id"]
    pack = json.loads((folder / "data.json").read_bytes())
    if mutation == "source":
        item = pack["evidence"][0]
        (cfg.raw_dir / "alpaca" / item["raw_file"]).write_bytes(b"{}")
    else:
        if mutation == "fact":
            pack["prices"][0]["close"] = "106"
        else:
            pack["coverage"][1]["missing_trading_dates"] = []
        (folder / "data.json").write_bytes(encoded(pack))
        manifest = json.loads((folder / "manifest.json").read_bytes())
        manifest["data_sha256"] = digest(encoded(pack))
        (folder / "manifest.json").write_bytes(encoded(manifest))
        pointer = json.loads((root / "current.json").read_bytes())
        pointer["manifest_sha256"] = digest(encoded(manifest))
        (root / "current.json").write_bytes(encoded(pointer))
    with pytest.raises(MarketUnavailable, match="integrity"):
        read(cfg)


def test_scope_includes_missing_issuers_and_multiple_listings():
    feed = {
        "data": [
            {"event": {"cik": "1"}, "company_id": "issuer-1", "listings": [{"ticker": "AAPL"}]}
        ],
        "missing_issuers": [
            {
                "cik": "2",
                "company_id": "issuer-2",
                "listings": [{"ticker": "BRK.B"}, {"ticker": "BRK.A"}],
            }
        ],
    }
    assert [item["ticker"] for item in scope_from_feed(feed)] == ["AAPL", "BRK.A", "BRK.B"]


@freeze_time("2024-08-01T12:00:00Z")
def test_global_page_bound_and_future_observations(tmp_path):  # type: ignore[no-untyped-def]
    client = AlpacaClient(
        cfg_for(tmp_path, alpaca_max_pages=1),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"bars": {"AAPL": [BAR]}, "next_page_token": "next"})
        ),
    )
    with pytest.raises(PayloadError, match="global page bound"):
        client.pages_for("prices", PARAMS, {"AAPL"}, lambda *_: "ref")
    client.close()
    client = AlpacaClient(
        cfg_for(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"bars": {"AAPL": [{**BAR, "t": "2024-08-02T04:00:00Z"}]}}
            )
        ),
    )
    with pytest.raises(PayloadError, match="Future"):
        client.pages_for("prices", PARAMS, {"AAPL"}, lambda *_: "ref")
    client.close()


@freeze_time("2024-08-01T12:00:00Z")
def test_empty_universe_never_publishes(tmp_path):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path)

    def empty_prices(request):  # type: ignore[no-untyped-def]
        if request.url.path == "/v2/stocks/bars":
            return httpx.Response(200, json={"bars": {}})
        return response_for(request)

    with pytest.raises(MarketUnavailable):
        publish(cfg, empty_prices)
    assert not (market_root(cfg) / "current.json").exists()
    assert read(cfg)["data"] is None
