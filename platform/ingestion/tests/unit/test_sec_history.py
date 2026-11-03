from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.adapters.sec_history import complete_submissions
from wtf_ingestion.adapters.sec_native import parse_submissions

CIK = "0000320193"
OBSERVED = datetime(2026, 10, 1, tzinfo=UTC)
FIELDS = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")


def inputs():  # type: ignore[no-untyped-def]
    recent = {
        key: [value]
        for key, value in zip(
            FIELDS,
            ("0000320193-26-000001", "2026-09-01", "8-K", "8.01", "release.htm"),
            strict=True,
        )
    }
    archive = {
        key: [value]
        for key, value in zip(
            FIELDS,
            ("0000320193-25-000001", "2025-10-01", "8-K", "2.02", "release.htm"),
            strict=True,
        )
    }
    payload = {
        "cik": "320193",
        "filings": {
            "recent": recent,
            "files": [
                {
                    "name": f"CIK{CIK}-submissions-001.json",
                    "filingFrom": "2025-10-01",
                    "filingTo": "2025-10-30",
                    "filingCount": 1,
                }
            ],
        },
    }
    return payload, archive


def test_declared_history_merges_and_retains_explicit_urls():
    payload, archive = inputs()
    fetch = Mock(return_value=json.dumps(archive).encode())
    result = complete_submissions(
        json.dumps(payload).encode(), CIK, OBSERVED, fetch, lookback_days=366
    )
    fetch.assert_called_once_with(f"https://data.sec.gov/submissions/CIK{CIK}-submissions-001.json")
    assert len(parse_submissions(result, CIK, OBSERVED, lookback_days=366)) == 2
    assert json.loads(result)["merged_archive_urls"] == [fetch.call_args.args[0]]


@pytest.mark.parametrize(
    "kind",
    [
        "unsafe",
        "wrong_count",
        "empty",
        "partial",
        "out_of_range",
        "conflict",
        "future",
        "malformed",
    ],
)
def test_archive_failures_never_silently_pass(kind):  # type: ignore[no-untyped-def]
    payload, archive = inputs()
    if kind == "unsafe":
        payload["filings"]["files"][0]["name"] = "../../private.json"
    elif kind == "wrong_count":
        payload["filings"]["files"][0]["filingCount"] = 2
    elif kind == "empty":
        archive = {key: [] for key in FIELDS}
    elif kind == "partial":
        archive["items"] = []
    elif kind == "out_of_range":
        archive["filingDate"] = ["2024-01-01"]
    elif kind == "conflict":
        archive["accessionNumber"] = payload["filings"]["recent"]["accessionNumber"]
    elif kind == "future":
        payload["filings"]["files"][0]["filingTo"] = "2027-01-01"
    with pytest.raises(PayloadError):
        complete_submissions(
            json.dumps(payload).encode(),
            CIK,
            OBSERVED,
            Mock(return_value=b"bad-json" if kind == "malformed" else json.dumps(archive).encode()),
            lookback_days=366,
        )


def test_old_archives_do_not_cause_extra_requests():
    payload, _ = inputs()
    raw = json.dumps(payload).encode()
    fetch = Mock()
    assert complete_submissions(raw, CIK, OBSERVED, fetch, lookback_days=120) == raw
    fetch.assert_not_called()
