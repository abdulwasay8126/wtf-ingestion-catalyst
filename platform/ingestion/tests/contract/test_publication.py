from __future__ import annotations

import csv
import json
from datetime import UTC, datetime

from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher


def test_jsonl_csv_manifest_are_consistent(db_session, fixture_root, tmp_path):  # type: ignore[no-untyped-def]
    observed = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
    candidate = SecEarningsAdapter(Settings()).parse_fixture(raw, observed)[0]
    IngestionService(db_session, tmp_path / "raw").ingest(
        source_key="sec_edgar",
        candidates=[candidate],
        raw=raw,
        source_url="https://www.sec.gov/Archives/edgar/data/fixture",
        content_type="application/json",
    )
    result = Publisher(db_session, tmp_path / "out").publish()
    jsonl = [
        json.loads(line)
        for line in (result.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    with (result.directory / "events.csv").open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    manifest = json.loads((result.directory / "manifest.json").read_text(encoding="utf-8"))
    assert len(jsonl) == len(csv_rows) == manifest["record_count"] == 1
    assert jsonl[0]["event_id"] == csv_rows[0]["event_id"]
    assert manifest["evidence_mode"] == "fixture"
    assert (tmp_path / "out/current.json").is_file()
