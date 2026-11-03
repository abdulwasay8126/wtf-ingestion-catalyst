from __future__ import annotations

import json

import pytest

from wtf_ingestion.operations.queries import coverage
from wtf_ingestion.universe import import_universe


def test_configurable_versioned_universe(db_session, tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "universe.json"
    payload = {
        "name": "Fixture fixed list",
        "version": "test-v2",
        "effective_from": "2026-10-01",
        "assumption": True,
        "companies": [
            {
                "ticker": "AAPL",
                "exchange": "NASDAQ",
                "company_name": "Apple Inc.",
                "cik": "0000320193",
            }
        ],
    }
    path.write_text(json.dumps(payload))
    assert import_universe(db_session, path) == "test-v2"
    assert import_universe(db_session, path) == "test-v2"
    assert coverage(db_session)["total"] == 1
    payload["companies"][0]["ticker"] = "CHANGED"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="cannot be overwritten"):
        import_universe(db_session, path)


def test_duplicate_universe_is_rejected(db_session, tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "universe.json"
    row = {
        "ticker": "AAPL",
        "exchange": "NASDAQ",
        "company_name": "Apple Inc.",
        "cik": "0000320193",
    }
    path.write_text(
        json.dumps(
            {
                "name": "Duplicate",
                "version": "invalid",
                "effective_from": "2026-10-01",
                "companies": [row, row],
            }
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        import_universe(db_session, path)
