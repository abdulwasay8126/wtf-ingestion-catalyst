from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import pytest

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.adapters.sec_estimates import METHOD, project_next_earnings

OBSERVED = datetime(2026, 10, 1, 12, tzinfo=UTC)


def submissions(rows):  # type: ignore[no-untyped-def]
    keys = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")
    return json.dumps(
        {
            "cik": "320193",
            "filings": {
                "recent": {key: [row[index] for row in rows] for index, key in enumerate(keys)}
            },
        }
    ).encode()


def project(raw, observed=OBSERVED):  # type: ignore[no-untyped-def]
    return project_next_earnings(
        raw,
        cik="0000320193",
        ticker="AAPL",
        exchange="NASDAQ",
        company_name="Sanitized issuer",
        observed=observed,
    )


def basis(day="2025-10-30", accession="0000320193-25-000001", form="8-K", items="2.02,9.01"):  # type: ignore[no-untyped-def]
    return (accession, day, form, items, "release.htm")


def test_projection_is_disclosed_date_only_with_raw_provenance():
    raw = submissions([basis()])
    result = project(raw)
    event = result.candidates[0]
    assert event.event_date == date(2026, 10, 29)
    assert event.status.value == "estimated"
    assert event.parser_version == METHOD
    assert event.scheduled_start is None and event.fiscal_period is None
    assert event.session.value == "UNKNOWN"
    assert event.metadata["estimate_confidence"] == "low"
    assert event.metadata["basis_is_filing_date_proxy"] is True
    assert event.metadata["estimate_basis_raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert str(event.source_url).endswith("000032019325000001/0000320193-25-000001-index.html")
    assert event.metadata["estimate_basis_url_kind"] == "filing_index"
    assert event.metadata["estimate_basis_primary_document"] == "release.htm"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [basis(form="8-K/A")],
        [basis(form="10-Q")],
        [basis(items="8.01")],
        [basis(day="2025-06-30")],
        [basis(day="2026-01-02")],
    ],
)
def test_no_basis_is_explicit_not_fabricated(rows):  # type: ignore[no-untyped-def]
    result = project(submissions(rows))
    assert result.candidates == []
    assert result.reason == "no_prior_year_item_2_02_basis"


def test_nearby_competing_bases_are_withheld():
    result = project(submissions([basis(), basis("2025-11-02", "0000320193-25-000002")]))
    assert not result.candidates and result.reason == "ambiguous_nearby_item_2_02_bases"


def test_next_basis_only_and_exact_duplicates_are_idempotent():
    result = project(submissions([basis(), basis(), basis("2025-12-15", "0000320193-25-000002")]))
    assert len(result.candidates) == 1 and result.candidates[0].event_date == date(2026, 10, 29)


@pytest.mark.parametrize(
    "raw",
    [
        b"broken",
        submissions([basis(day="2027-01-01")]),
        submissions([basis(items=None)]),
        submissions([(*basis()[:4], "../../escape.htm")]),
        submissions([basis(), basis(day="2025-11-15")]),
    ],
)
def test_malformed_future_path_and_conflicting_duplicate_fail(raw):  # type: ignore[no-untyped-def]
    with pytest.raises(PayloadError):
        project(raw)


def test_naive_observation_fails_and_partial_arrays_fail():
    with pytest.raises(PayloadError, match="timezone"):
        project(submissions([basis()]), datetime(2026, 10, 1))
    payload = json.loads(submissions([basis()]))
    payload["filings"]["recent"]["items"] = []
    with pytest.raises(PayloadError):
        project(json.dumps(payload).encode())
