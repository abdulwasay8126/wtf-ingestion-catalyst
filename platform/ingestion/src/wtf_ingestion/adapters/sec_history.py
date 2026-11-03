from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from wtf_ingestion.adapters.base import PayloadError

FIELDS = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")


def complete_submissions(
    raw: bytes,
    cik: str,
    observed: datetime,
    fetch_archive: Callable[[str], bytes],
    lookback_days: int = 364,
) -> bytes:
    """Merge only SEC-declared archive files needed for the reviewed lookback; no guessed URLs."""
    try:
        payload: dict[str, Any] = json.loads(raw)
        if str(payload["cik"]).zfill(10) != cik:
            raise PayloadError("SEC issuer identity mismatch")
        recent = payload["filings"]["recent"]
        if not all(isinstance(recent[key], list) for key in FIELDS):
            raise PayloadError("SEC recent fields must be arrays")
        if len({len(recent[key]) for key in FIELDS}) != 1:
            raise PayloadError("SEC parallel arrays have inconsistent lengths")
        cutoff = (observed - timedelta(days=lookback_days)).date()
        archives = payload["filings"].get("files", [])
        if not isinstance(archives, list):
            raise PayloadError("SEC archive metadata must be an array")
        selected = []
        for item in archives:
            name = item["name"]
            if not re.fullmatch(rf"CIK{cik}-submissions-\d{{3}}\.json", name):
                raise PayloadError("SEC declared archive filename is unsafe or mismatched")
            start, end = (
                datetime.fromisoformat(item["filingFrom"]).date(),
                datetime.fromisoformat(item["filingTo"]).date(),
            )
            if start > end or end > observed.date():
                raise PayloadError("SEC archive range invalid")
            if end >= cutoff:
                selected.append(item)
        if not selected:
            return raw
        if len(selected) > 20:
            raise PayloadError("SEC archive lookback exceeds reviewed per-issuer bound")
        rows = [tuple(recent[key][i] for key in FIELDS) for i in range(len(recent["form"]))]
        evidence = []
        for item in sorted(selected, key=lambda value: value["name"]):
            url = "https://data.sec.gov/submissions/" + item["name"]
            archive = json.loads(fetch_archive(url))
            if not all(isinstance(archive[key], list) for key in FIELDS):
                raise PayloadError("SEC archive fields must be arrays")
            lengths = {len(archive[key]) for key in FIELDS}
            if len(lengths) != 1 or lengths == {0} or len(archive["form"]) != item["filingCount"]:
                raise PayloadError("SEC declared archive count mismatch or incomplete input")
            for i in range(len(archive["form"])):
                filed = datetime.fromisoformat(archive["filingDate"][i]).date()
                if (
                    not datetime.fromisoformat(item["filingFrom"]).date()
                    <= filed
                    <= datetime.fromisoformat(item["filingTo"]).date()
                ):
                    raise PayloadError("SEC archive row outside declared range")
                rows.append(tuple(archive[key][i] for key in FIELDS))
            evidence.append(url)
        unique: dict[str, tuple[Any, ...]] = {}
        for row in rows:
            accession = row[0]
            if accession in unique and unique[accession] != row:
                raise PayloadError("conflicting SEC archive/recent accession")
            unique[accession] = row
        payload["filings"]["recent"] = {
            key: [row[i] for row in unique.values()] for i, key in enumerate(FIELDS)
        }
        payload["merged_archive_urls"] = evidence
        return json.dumps(payload, sort_keys=True).encode()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PayloadError("malformed SEC archive metadata or parallel arrays") from exc
