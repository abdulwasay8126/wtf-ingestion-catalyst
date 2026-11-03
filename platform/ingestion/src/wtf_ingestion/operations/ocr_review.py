"""Read-only local OCR evidence. No database mutation or event-fact acceptance."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


def read_json(path: Path, limit: int = 2_000_000) -> dict[str, Any]:
    if path.stat().st_size > limit:
        raise ValueError("OCR review metadata exceeds its size bound")
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result


def review_data(root: Path) -> dict[str, Any] | None:
    pointer = root / "results/latest-complete.json"
    if not pointer.is_file():
        return None
    filename = read_json(pointer, 4096)["report_file"]
    if not re.fullmatch(r"report-[0-9]+\.json", filename):
        raise ValueError("OCR report reference is invalid")
    report = read_json(root / "results" / filename)
    directory = str(UUID(report["capture_directory"]))
    capture_path = root / directory / "capture.json"
    if capture_path.stat().st_size > 2_000_000:
        raise ValueError("OCR capture exceeds its size bound")
    capture_bytes = capture_path.read_bytes()
    if hashlib.sha256(capture_bytes).hexdigest() != report["capture_sha256"]:
        raise ValueError("OCR capture changed after recognition")
    capture = json.loads(capture_bytes)
    if capture["state"] != "CAPTURED_REVIEW_ONLY" or not report["complete_capture"]:
        raise ValueError("OCR capture/recognition is incomplete")
    expected = {}
    for document in capture["documents"]:
        pages = document["pages"]
        if not document["complete_capture"] or [page["page_number"] for page in pages] != list(
            range(1, document["total_pages"] + 1)
        ):
            raise ValueError("OCR document pages are incomplete")
        for page in pages:
            expected[(document["ticker"], page["page_number"])] = page
    if len(expected) != len(report["pages"]) or len(expected) > 450:
        raise ValueError("OCR report page coverage is incomplete")
    rows = []
    seen = set()
    for page in report["pages"]:
        identity = (page["ticker"], page["page_number"])
        if identity not in expected or identity in seen:
            raise ValueError("OCR report has unknown or duplicate pages")
        seen.add(identity)
        original = expected[identity]
        cache = page["cache_file"]
        if not re.fullmatch(r"[a-f0-9]{64}\.json", cache):
            raise ValueError("OCR cache reference is invalid")
        result = read_json(root / "results" / cache)
        if (
            result["context"] != report["engine"]
            or result["input_sha256"] != original["sha256"]
            or hashlib.sha256(result["text"].encode()).hexdigest() != result["text_sha256"]
        ):
            raise ValueError("OCR result context/checksum mismatch")
        url = urlsplit(original["url"])
        if (
            url.scheme != "https"
            or url.netloc != "www.sec.gov"
            or not url.path.startswith("/Archives/edgar/data/")
            or url.query
            or url.fragment
        ):
            raise ValueError("OCR source image URL is invalid")
        rows.append(
            {
                **page,
                "image_url": original["url"],
                "retrieved_at": original["retrieved_at"],
                "text": result["text"],
                "sha256": original["sha256"],
            }
        )
    automatic = root.parent / "ocr-auto" / "diagnostic.json"
    quality = None
    if automatic.is_file():
        quality = read_json(automatic)
        if quality["capture_sha256"] != report["capture_sha256"]:
            raise ValueError("Automatic OCR quality report belongs to another capture")
        by_page = {(p["ticker"], p["page"]): p for p in quality["pages"]}
        if len(by_page) != len(quality["pages"]) or set(by_page) != set(expected):
            raise ValueError("Automatic OCR quality report has incomplete page coverage")
        for row in rows:
            page = by_page[(row["ticker"], row["page_number"])]
            if page.get("error"):
                row["quality_readable"] = False
                continue
            filename = page["cache_file"]
            key = hashlib.sha256(
                (row["sha256"] + json.dumps(page["context"], sort_keys=True)).encode()
            ).hexdigest()
            if filename != key + ".json" or page["input_sha256"] != row["sha256"]:
                raise ValueError("Automatic OCR cache identity mismatch")
            recognized = read_json(automatic.parent / filename)
            if (
                recognized["input_sha256"] != row["sha256"]
                or recognized["context"] != page["context"]
                or recognized["readable"] != page["readable"]
            ):
                raise ValueError("Automatic OCR context/checksum mismatch")
            for layout in recognized["layouts"]:
                if hashlib.sha256(layout["text"].encode()).hexdigest() != layout["text_sha256"]:
                    raise ValueError("Automatic OCR text checksum mismatch")
            best = max(
                recognized["layouts"], key=lambda item: item["mean_confidence"], default=None
            )
            row.update(
                text=best["text"] if best else "[Pixel-verified decorative/blank page]",
                word_count=best["words"] if best else 0,
                mean_word_confidence=best["mean_confidence"] if best else None,
                quality_readable=recognized["readable"],
            )
    return {"report": report, "pages": rows, "capture_directory": directory, "quality": quality}


def review_image(root: Path, directory: UUID, digest: str) -> tuple[Path, str] | None:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        return None
    capture_path = root / str(directory) / "capture.json"
    if not capture_path.is_file():
        return None
    capture = read_json(capture_path)
    for document in capture["documents"]:
        for page in document["pages"]:
            if page["sha256"] != digest or page["file"] != digest + ".bin":
                continue
            path = root / str(directory) / page["file"]
            if path.stat().st_size > 12_000_000:
                raise ValueError("OCR source image is oversized")
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("OCR source image checksum mismatch")
            if body.startswith(b"\xff\xd8\xff"):
                return path, "image/jpeg"
            if body.startswith(b"\x89PNG\r\n\x1a\n"):
                return path, "image/png"
            raise ValueError("OCR source image signature is invalid")
    return None
