from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from wtf_ingestion.config import Settings
from wtf_ingestion.earnings_history import (
    CONTRACT,
    METHOD,
    HistoryUnavailable,
    actual_releases,
    cover_identity,
    extract_history,
    history_root,
    price_reaction,
    read_history,
)
from wtf_ingestion.market_data import atomic, digest, encoded

OBSERVED = datetime(2026, 7, 29, 15, tzinfo=UTC)
STATEMENT = (
    'On July 28, 2026, Example Corp. (the "Company") issued a press release reporting '
    "its financial results for the quarter ended June 30, 2026. "
    "The Company will hold a conference call on July 29, 2026 at 9:00 a.m. ET."
)
LISTING = {"ticker": "ABC", "cik": "0000000001", "exchange": "NYSE", "company_id": "issuer"}


@pytest.mark.parametrize(
    "statement",
    [
        STATEMENT,
        "On July 28, 2026, Example, Inc. reported its second quarter 2026 net income.",
        "On 28 July 2026, Example issued an earnings press release for the quarter.",
        "On July 28, 2026, Example issued a press release announcing "
        "its results for the quarter ended June 30, 2026.",
        "Results of operations for second quarter 2026. "
        "The press release was issued on July 28, 2026.",
        "Attached is the Company's press release, dated July 28, 2026, "
        "reporting financial results.",
    ],
)
def test_actual_source_date_is_not_filing_or_period_or_call_date(statement: str) -> None:
    rows = actual_releases(statement, date(2026, 7, 29), OBSERVED)
    assert len(rows) == 1
    assert rows[0]["actual_release_date"] == "2026-07-28"
    assert rows[0]["actual_release_at"] is None
    assert "July 28" in rows[0]["excerpt"] or "28 July" in rows[0]["excerpt"]


@pytest.mark.parametrize(
    "statement",
    [
        "On July 28, 2026, Example announced that it will report "
        "financial results on August 4, 2026.",
        "On July 28, 2026, Example announced an earnings conference call for August 4, 2026.",
        "Quarter ended June 30, 2026. Date of report July 28, 2026. Financial results attached.",
        "Example reported results on July 28 without a supported year.",
        "On July 28, 2026, Example issued earnings guidance for the coming quarter.",
        "On July 30, 2026, Example reported financial results.",
        "On February 30, 2026, Example reported financial results.",
    ],
)
def test_no_invention_from_unsupported_or_future_statements(statement: str) -> None:
    if "February 30" in statement:
        with pytest.raises(ValueError):
            actual_releases(statement, date(2026, 7, 29), OBSERVED)
    else:
        assert actual_releases(statement, date(2026, 7, 29), OBSERVED) == []


def test_cover_registered_symbol_and_conflicting_issuer() -> None:
    raw = (
        b'<html><body><ix:nonNumeric name="dei:EntityCentralIndexKey">1</ix:nonNumeric>'
        b'<ix:nonNumeric name="dei:TradingSymbol">ABC</ix:nonNumeric></body></html>'
    )
    assert cover_identity(raw, LISTING["cik"]) == ["ABC"]
    with pytest.raises(HistoryUnavailable):
        cover_identity(raw, "0000000002")


def event(session: str = "UNKNOWN") -> dict:
    return {
        "historical_event_id": "event",
        "cik": LISTING["cik"],
        "actual_release_date": "2026-07-28",
        "session": session,
        "support_state": "SUPPORTED_ACTUAL",
        "source_assertions": [{"cover_tickers": ["ABC"]}],
    }


def market() -> dict:
    days = ["2026-07-27", "2026-07-28", "2026-07-29"]
    return {
        "prices": [
            {
                "ticker": "ABC",
                "trading_date": d,
                "close": str(p),
                "adjustment": "all",
                "currency": "USD",
                "feed": "sip",
                "evidence_id": d,
            }
            for d, p in zip(days, [100, 110, 121], strict=True)
        ],
        "trading_calendar": [{"date": d} for d in days],
        "corporate_actions": [],
    }


@pytest.mark.parametrize(
    ("session", "expected", "first", "last"),
    [
        ("UNKNOWN", "21.000000", "2026-07-27", "2026-07-29"),
        ("BMO", "10.000000", "2026-07-27", "2026-07-28"),
        ("AMC", "10.000000", "2026-07-28", "2026-07-29"),
    ],
)
def test_hand_checkable_decimal_returns(session: str, expected: str, first: str, last: str) -> None:
    move = price_reaction(event(session), LISTING, market())
    assert move["state"] == "AVAILABLE"
    assert move["return_percent"] == expected
    assert (move["baseline_date"], move["measurement_date"]) == (first, last)


def test_closed_release_day_uses_enclosing_sessions() -> None:
    e, m = event(), market()
    e["actual_release_date"] = "2026-07-26"
    m["trading_calendar"][0]["date"] = "2026-07-24"
    m["prices"][0]["trading_date"] = "2026-07-24"
    assert price_reaction(e, LISTING, m)["return_percent"] == "10.000000"


@pytest.mark.parametrize(
    "damage",
    ["missing", "duplicate", "zero", "currency", "identity", "conflict", "merger", "outside"],
)
def test_incomplete_or_unqualified_inputs_abstain(damage: str) -> None:
    e, m = event(), market()
    if damage == "missing":
        m["prices"].pop(1)
    elif damage == "duplicate":
        m["prices"].append(copy.deepcopy(m["prices"][1]))
    elif damage == "zero":
        m["prices"][0]["close"] = "0"
    elif damage == "currency":
        m["prices"][0]["currency"] = "EUR"
    elif damage == "identity":
        e["source_assertions"][0]["cover_tickers"] = ["DEF"]
    elif damage == "conflict":
        e["support_state"] = "CONFLICT"
    elif damage == "merger":
        m["corporate_actions"] = [{"tickers": ["ABC"], "type": "stock_mergers"}]
    elif damage == "outside":
        e["actual_release_date"] = "2026-08-01"
    result = price_reaction(e, LISTING, m)
    assert result["state"] == "UNAVAILABLE" and result["return_percent"] is None
    assert result["reason_code"]


def test_native_bundle_original_clocks_and_hashes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = Settings(raw_dir=tmp_path)
    raw = (
        '<html><body><ix:nonNumeric name="dei:TradingSymbol">ABC</ix:nonNumeric>'
        f"{STATEMENT}</body></html>"
    ).encode()
    body_sha = digest(raw)
    (tmp_path / (body_sha + ".bin")).write_bytes(raw)
    doc = {
        "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/primary.htm",
        "body_sha256": body_sha,
        "retrieved_at": OBSERVED.isoformat(),
        "filing": {
            "cik": LISTING["cik"],
            "accession": "0000000001-26-000001",
            "filed_on": "2026-07-29",
            "form": "8-K",
            "primary_document": "primary.htm",
            "items": "2.02,9.01",
        },
    }
    bundle = encoded({"complete": True, "bundle_format": "raw_sha256_refs_v1", "documents": [doc]})
    bundle_sha = digest(bundle)
    (tmp_path / (bundle_sha + ".bin")).write_bytes(bundle)
    history = extract_history(cfg, bundle_sha, [LISTING])
    assert history["events"][0]["source_assertions"][0]["observed_at"] == OBSERVED.isoformat()
    assert history["events"][0]["actual_release_date"] == "2026-07-28"
    (tmp_path / (body_sha + ".bin")).write_bytes(raw + b"changed")
    with pytest.raises(RuntimeError):
        extract_history(cfg, bundle_sha, [LISTING])


def test_missing_history_has_no_side_effects(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = Settings(output_dir=tmp_path / "publications")
    result = read_history(cfg, calendar_publication_id="calendar", calendar_manifest_sha256="hash")
    assert result["history_state"] == "UNAVAILABLE" and result["data"] is None
    assert not history_root(cfg).exists()


def test_tampered_pointer_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = Settings(output_dir=tmp_path / "publications")
    atomic(history_root(cfg) / "current.json", {})
    with pytest.raises(HistoryUnavailable):
        read_history(cfg, calendar_publication_id="calendar", calendar_manifest_sha256="hash")


@pytest.fixture
def history_publication(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    cfg = Settings(output_dir=tmp_path / "publications", raw_dir=tmp_path / "raw")
    cfg.raw_dir.mkdir()
    raw = (
        f'<html><ix:nonNumeric name="dei:TradingSymbol">ABC</ix:nonNumeric>{STATEMENT}</html>'
    ).encode()
    sha = digest(raw)
    (cfg.raw_dir / (sha + ".bin")).write_bytes(raw)
    bundle = {
        "complete": True,
        "bundle_format": "raw_sha256_refs_v1",
        "documents": [
            {
                "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/primary.htm",
                "body_sha256": sha,
                "retrieved_at": OBSERVED.isoformat(),
                "filing": {
                    "cik": LISTING["cik"],
                    "accession": "0000000001-26-000001",
                    "filed_on": "2026-07-29",
                    "form": "8-K",
                    "primary_document": "primary.htm",
                    "items": "2.02,9.01",
                },
            }
        ],
    }
    bundle_sha = digest(encoded(bundle))
    (cfg.raw_dir / (bundle_sha + ".bin")).write_bytes(encoded(bundle))
    source = extract_history(cfg, bundle_sha, [LISTING])
    m = {
        **market(),
        "scope": [LISTING],
        "market_publication_id": "market",
        "trust_deadline": "2026-07-30T15:00:00+00:00",
    }
    market_state = {"is_current": True, "market_state": "HEALTHY", "data": m}
    monkeypatch.setattr("wtf_ingestion.earnings_history.read_market", lambda *a, **k: market_state)
    identifier = str(uuid4())
    pack = {
        "contract_version": CONTRACT,
        "method_version": METHOD,
        "history_publication_id": identifier,
        "calendar_publication_id": "calendar",
        "calendar_manifest_sha256": "hash",
        "market_publication_id": "market",
        "source_bundle_sha256": bundle_sha,
        "scope": [LISTING],
        "scope_sha256": digest(encoded([LISTING])),
        "evidence_mode": "fixture",
        "market_trust_deadline": m["trust_deadline"],
        **source,
        "reactions": [price_reaction(source["events"][0], LISTING, m)],
        "coverage": [{"ticker": "ABC", "actual_events": 1, "available_reactions": 1}],
    }
    folder = history_root(cfg) / "publications" / identifier

    def write():
        manifest = {
            "contract_version": CONTRACT,
            "history_publication_id": identifier,
            "calendar_publication_id": "calendar",
            "market_publication_id": "market",
            "data_sha256": digest(encoded(pack)),
        }
        atomic(folder / "data.json", pack)
        atomic(folder / "manifest.json", manifest)
        atomic(
            history_root(cfg) / "current.json",
            {
                "history_publication_id": identifier,
                "manifest_sha256": digest(encoded(manifest)),
            },
        )
        atomic(
            history_root(cfg) / "status.json",
            {
                "state": "SUCCEEDED",
                "history_publication_id": identifier,
            },
        )

    write()
    return cfg, pack, write, market_state, sha


@pytest.mark.parametrize("damage", ["event", "return", "coverage", "source"])
def test_history_tamper_refused_even_after_hashes_rewritten(history_publication, damage):
    cfg, pack, write, _, sha = history_publication
    if damage == "event":
        pack["events"][0]["actual_release_date"] = "2026-07-27"
    elif damage == "return":
        pack["reactions"][0]["return_percent"] = "99.000000"
    elif damage == "coverage":
        pack["coverage"][0]["available_reactions"] = 2
    else:
        (cfg.raw_dir / (sha + ".bin")).write_bytes(b"changed source")
    write()
    with pytest.raises(HistoryUnavailable, match="integrity"):
        read_history(cfg, calendar_publication_id="calendar", calendar_manifest_sha256="hash")


def test_history_stale_failed_and_superseded_keep_audit_separate(history_publication):
    cfg, _, _, m, _ = history_publication

    def read(**kwargs):
        return read_history(
            cfg, calendar_publication_id="calendar", calendar_manifest_sha256="hash", **kwargs
        )

    assert read(ticker="ABC")["data"]["reactions"][0]["return_percent"] == "21.000000"
    assert read(ticker="MISSING")["data"]["events"] == []
    m.update(is_current=False, market_state="STALE")
    assert read()["data"] is None and read()["history_state"] == "STALE"
    assert read(retained=True)["data"] and not read(retained=True)["is_current"]
    atomic(history_root(cfg) / "status.json", {"state": "FAILED"})
    assert read()["history_state"] == "REFRESH_FAILED"
    m["data"]["market_publication_id"] = "replacement"
    assert read(retained=True)["history_state"] == "SUPERSEDED"
