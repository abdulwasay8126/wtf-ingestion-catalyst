from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.adapters.economic import IcsEconomicAdapter
from wtf_ingestion.adapters.ir import IssuerIrHtmlAdapter
from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings

OBSERVED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_sec_accepts_only_explicit_future_announcement(fixture_root):  # type: ignore[no-untyped-def]
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    records = SecEarningsAdapter(Settings()).parse_fixture(raw, OBSERVED)
    assert len(records) == 1
    assert records[0].ticker == "AAPL"
    assert records[0].session.value == "AMC"
    assert records[0].scheduled_start is not None
    assert records[0].scheduled_start.tzinfo is not None


@pytest.mark.parametrize("name", ["malformed.json", "partial.json"])
def test_sec_malformed_or_partial_fails(fixture_root, name):  # type: ignore[no-untyped-def]
    raw = (fixture_root / "sec" / name).read_bytes()
    adapter = SecEarningsAdapter(Settings())
    if name == "malformed.json":
        with pytest.raises(PayloadError):
            adapter.parse_fixture(raw, OBSERVED)
    else:
        with pytest.raises((KeyError, PayloadError)):
            adapter.parse_fixture(raw, OBSERVED)


def test_live_sec_fails_without_approved_identity() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        SecEarningsAdapter(Settings()).fetch_submissions("320193")


def _ir_adapter() -> IssuerIrHtmlAdapter:
    return IssuerIrHtmlAdapter(
        source_id="issuer_demo",
        ticker="AMZN",
        exchange="NASDAQ",
        company_name="Amazon.com, Inc.",
        cik="1018724",
        source_url="https://investor.example.invalid/releases/q3",
    )


def test_ir_valid_and_not_yet_announced_are_distinct(fixture_root):  # type: ignore[no-untyped-def]
    valid = _ir_adapter().parse_fixture(
        (fixture_root / "ir/official_announcement.html").read_bytes(), OBSERVED
    )
    missing = _ir_adapter().parse_fixture(
        (fixture_root / "ir/not_yet_announced.html").read_bytes(), OBSERVED
    )
    assert len(valid) == 1
    assert missing == []


def test_ir_changed_layout_fails(fixture_root):  # type: ignore[no-untyped-def]
    with pytest.raises(PayloadError):
        _ir_adapter().parse_fixture(
            (fixture_root / "ir/changed_layout.html").read_bytes(), OBSERVED
        )


def test_ics_preserves_timezone_and_rejects_malformed(fixture_root):  # type: ignore[no-untyped-def]
    adapter = IcsEconomicAdapter(
        source_id="bls_calendar",
        owner="U.S. Bureau of Labor Statistics",
        source_url="https://www.bls.gov/schedule/news_release/bls.ics",
    )
    records = adapter.parse((fixture_root / "economic/bls.ics").read_bytes(), OBSERVED)
    assert len(records) == 2
    assert records[0].scheduled_start is not None
    assert records[0].scheduled_start.tzinfo == UTC
    with pytest.raises(PayloadError):
        adapter.parse((fixture_root / "economic/malformed.ics").read_bytes(), OBSERVED)


def test_ics_empty_is_visible_to_pipeline(fixture_root):  # type: ignore[no-untyped-def]
    adapter = IcsEconomicAdapter(
        source_id="bls_calendar",
        owner="BLS",
        source_url="https://www.bls.gov/schedule/news_release/bls.ics",
    )
    assert adapter.parse((fixture_root / "economic/empty.ics").read_bytes(), OBSERVED) == []
