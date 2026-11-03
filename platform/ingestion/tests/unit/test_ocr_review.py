from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest

from wtf_ingestion.operations.ocr_review import review_data, review_image


def fixture_pack(root: Path) -> tuple[str, str]:
    directory = str(uuid4())
    images = root / directory
    results = root / "results"
    images.mkdir(parents=True)
    results.mkdir()
    body = b"\x89PNG\r\n\x1a\nfixture image"
    digest = hashlib.sha256(body).hexdigest()
    (images / (digest + ".bin")).write_bytes(body)
    capture = {
        "state": "CAPTURED_REVIEW_ONLY",
        "documents": [
            {
                "ticker": "TEST",
                "total_pages": 1,
                "complete_capture": True,
                "pages": [
                    {
                        "page_number": 1,
                        "sha256": digest,
                        "file": digest + ".bin",
                        "url": "https://www.sec.gov/Archives/edgar/data/1/1/page.png",
                        "retrieved_at": "2026-10-02T12:00:00+00:00",
                    }
                ],
            }
        ],
    }
    capture_path = images / "capture.json"
    capture_path.write_text(json.dumps(capture))
    cache = "a" * 64 + ".json"
    context = {"engine": "fixture"}
    text = '<script>alert("untrusted source")</script>'
    (results / cache).write_text(
        json.dumps(
            {
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "input_sha256": digest,
                "context": context,
            }
        )
    )
    report = {
        "capture_directory": directory,
        "capture_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
        "engine": context,
        "complete_capture": True,
        "elapsed_seconds": 1.0,
        "pages": [
            {
                "ticker": "TEST",
                "page_number": 1,
                "cache_file": cache,
                "total_pages": 1,
                "word_count": 4,
                "mean_word_confidence": 90,
            }
        ],
    }
    (results / "report-1.json").write_text(json.dumps(report))
    (results / "latest-complete.json").write_text(json.dumps({"report_file": "report-1.json"}))
    return directory, digest


def test_ocr_review_preserves_text_but_never_accepts_it(tmp_path: Path) -> None:
    from uuid import UUID

    directory, digest = fixture_pack(tmp_path)
    data = review_data(tmp_path)
    assert data is not None and len(data["pages"]) == 1
    assert "<script>" in data["pages"][0]["text"]
    image = review_image(tmp_path, UUID(directory), digest)
    assert image is not None and image[1] == "image/png"
    assert review_image(tmp_path, UUID(directory), "../secret") is None


def test_ocr_review_uses_capture_url_not_untrusted_report_link(tmp_path: Path) -> None:
    fixture_pack(tmp_path)
    path = tmp_path / "results/report-1.json"
    report = json.loads(path.read_text())
    report["pages"][0]["image_url"] = "javascript:alert(1)"
    path.write_text(json.dumps(report))
    data = review_data(tmp_path)
    assert data and data["pages"][0]["image_url"].startswith("https://www.sec.gov/")


@pytest.mark.parametrize("fault", [None, "capture", "missing", "identity", "context", "text"])
def test_current_quality_text_requires_complete_verified_evidence(tmp_path: Path, fault) -> None:
    root = tmp_path / "ocr-trial"
    directory, digest = fixture_pack(root)
    context = {"version": "fixture-v4"}
    key = hashlib.sha256((digest + json.dumps(context, sort_keys=True)).encode()).hexdigest()
    text = "Updated retained recognition"
    recognized = {
        "input_sha256": digest,
        "context": context,
        "readable": True,
        "layouts": [
            {
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "words": 3,
                "mean_confidence": 95,
            }
        ],
    }
    page = {
        "ticker": "TEST",
        "page": 1,
        "readable": True,
        "input_sha256": digest,
        "context": context,
        "cache_file": key + ".json",
    }
    quality = {
        "capture_sha256": hashlib.sha256(
            (root / directory / "capture.json").read_bytes()
        ).hexdigest(),
        "pages": [page],
        "unreadable_pages": [],
        "completed_at": "2026-10-03T04:00:00+00:00",
        "elapsed_seconds": 1,
    }
    if fault == "capture":
        quality["capture_sha256"] = "changed"
    elif fault == "missing":
        quality["pages"] = []
    elif fault == "identity":
        page["cache_file"] = "../not-approved.json"
    elif fault == "context":
        recognized["context"] = {"version": "changed"}
    elif fault == "text":
        recognized["layouts"][0]["text"] = "changed"
    automatic = tmp_path / "ocr-auto"
    automatic.mkdir()
    (automatic / (key + ".json")).write_text(json.dumps(recognized))
    (automatic / "diagnostic.json").write_text(json.dumps(quality))
    if fault:
        with pytest.raises(ValueError):
            review_data(root)
    else:
        data = review_data(root)
        assert data and data["pages"][0]["text"] == text
        assert data["pages"][0]["quality_readable"]


@pytest.mark.parametrize("fault", ["capture", "text", "context", "missing", "duplicate", "path"])
def test_ocr_review_rejects_changed_or_incomplete_evidence(tmp_path: Path, fault: str) -> None:
    directory, _ = fixture_pack(tmp_path)
    results = tmp_path / "results"
    if fault == "capture":
        (tmp_path / directory / "capture.json").write_text("{}")
    elif fault in {"text", "context"}:
        path = results / ("a" * 64 + ".json")
        cache = json.loads(path.read_text())
        cache[fault] = "changed"
        path.write_text(json.dumps(cache))
    elif fault == "path":
        (results / "latest-complete.json").write_text(json.dumps({"report_file": "../secret.json"}))
    else:
        path = results / "report-1.json"
        report = json.loads(path.read_text())
        report["pages"] = [] if fault == "missing" else report["pages"] * 2
        path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        review_data(tmp_path)


def test_offline_recognizer_requires_complete_ordered_pages() -> None:
    spec = importlib.util.spec_from_file_location("ocr_recognizer", Path("tools/ocr/recognize.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_document(
        {
            "total_pages": 2,
            "complete_capture": True,
            "pages": [{"page_number": 1}, {"page_number": 2}],
        }
    )
    for pages in ([1], [2, 1], [1, 1], [1, 3]):
        with pytest.raises(ValueError):
            module.validate_document(
                {
                    "total_pages": 2,
                    "complete_capture": True,
                    "pages": [{"page_number": number} for number in pages],
                }
            )
