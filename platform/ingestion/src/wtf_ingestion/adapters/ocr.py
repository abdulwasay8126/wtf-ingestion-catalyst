"""Bounded offline OCR, with retained alternatives and explicit quality failures.

Recognition is not a promise of arbitrary-layout accuracy. A page must meet the
quality checks below; callers must compare parsed facts across both text layouts.
No dates or fiscal periods are repaired or inferred here.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re

# The local OCR/PDF tools are allowlisted, bounded and executed without a shell.
import subprocess  # nosec B404
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from wtf_ingestion.adapters.base import PayloadError, UnreadableDocumentError
from wtf_ingestion.publication.files import replace_atomic

OCR_VERSION = "local-ocr-v4"


class OcrQualityError(UnreadableDocumentError):
    code = "OCR_QUALITY_REVIEW_REQUIRED"


def _run(arguments: list[str], timeout: int = 45) -> bytes:
    if not arguments or arguments[0] not in {
        "/usr/bin/tesseract",
        "/usr/bin/pdfinfo",
        "/usr/bin/pdftoppm",
    }:
        raise OcrQualityError("Local OCR executable is not approved")
    try:
        result = subprocess.run(  # noqa: S603 # nosec B603
            arguments,
            check=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcrQualityError("Local OCR engine failed or exceeded its time limit") from exc
    if len(result.stdout) > 2_000_000:
        raise OcrQualityError("OCR output exceeds its size limit")
    return result.stdout


@lru_cache(maxsize=1)
def engine_context() -> dict[str, str]:
    language = Path("/usr/share/tesseract-ocr/5/tessdata")
    try:
        hashes = ":".join(
            hashlib.sha256((language / f"{name}.traineddata").read_bytes()).hexdigest()
            for name in ("eng", "chi_tra")
        )
    except OSError as exc:
        raise OcrQualityError("OCR language files are not installed") from exc
    return {
        "version": OCR_VERSION,
        "engine": _run(["/usr/bin/tesseract", "--version"]).decode().splitlines()[0],
        "language_sha256": hashes,
        "english_sha256": hashes.split(":")[0],
        "best_sha256": ":".join(
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path("/opt/ocr-best/eng.traineddata"),
                Path("/opt/ocr-best/chi_tra.traineddata"),
            )
            if path.is_file()
        ),
    }


def readable_layouts(layouts: list[dict[str, Any]]) -> bool:
    return any(
        item["words"] >= 1
        and item["mean_confidence"] >= 80
        and item["low_confidence_words"] <= item["words"] * 0.2
        for item in layouts
    )


def checked_layouts(layouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject changed text; discard pre-fix TSV quote artifacts for re-recognition."""
    clean = []
    for item in layouts:
        if hashlib.sha256(item["text"].encode()).hexdigest() != item["text_sha256"]:
            raise OcrQualityError("OCR text checksum mismatch")
        if "\t" not in item["text"]:
            clean.append(item)
    return clean


def blank_image(image: Image.Image) -> bool:
    if max(ImageStat.Stat(image).stddev) < 0.5:
        return True
    mask = ImageChops.difference(image, Image.new("RGB", image.size, "white"))
    box = mask.convert("L").point(lambda x: 255 if x > 30 else 0).getbbox()
    # A full-height uniform colour rectangle with uniform white margins is a
    # decorative page. An empty OCR result alone never enters this path.
    if not box or box[1] != 0 or box[3] != image.height or box[2] - box[0] < 20:
        return False
    interior = image.crop((box[0] + 4, 4, box[2] - 4, image.height - 4))
    if max(ImageStat.Stat(interior).stddev) >= 0.5:
        return False
    for edges in (
        (0, 0, max(0, box[0] - 8), image.height),
        (min(image.width, box[2] + 8), 0, image.width, image.height),
    ):
        if edges[2] > edges[0]:
            stats = ImageStat.Stat(image.crop(edges))
            if min(stats.mean) < 250 or max(stats.stddev) >= 0.5:
                return False
    return True


def legacy_layout(digest: str, context: dict[str, str], root: Path) -> dict[str, Any] | None:
    """Reuse a byte/model-identical retained trial pass, never its approval verdict."""
    old_context = {
        "engine": context["engine"],
        "language_sha256": context["english_sha256"],
        "language": "eng",
        "page_segmentation": 3,
        "program_version": "local-ocr-trial-v1",
    }
    key = hashlib.sha256(
        digest.encode() + json.dumps(old_context, sort_keys=True).encode()
    ).hexdigest()
    path = root.parent / "ocr-trial" / "results" / f"{key}.json"
    if not path.is_file() or path.stat().st_size > 2_000_000:
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        result["input_sha256"] != digest
        or result["context"] != old_context
        or hashlib.sha256(result["text"].encode()).hexdigest() != result["text_sha256"]
    ):
        raise OcrQualityError("Retained trial OCR checksum mismatch")
    if "\t" in result["text"]:
        return None
    return {
        "text": result["text"],
        "text_sha256": result["text_sha256"],
        "words": result["word_count"],
        "mean_confidence": result["mean_word_confidence"] or 0,
        # The earlier trial counted below 80; a conservative upper bound for below 60.
        "low_confidence_words": result["low_confidence_words"],
        "reused_trial": True,
    }


def parse_tsv(raw: bytes) -> dict[str, Any]:
    try:
        words = [
            row
            for row in csv.DictReader(
                io.StringIO(raw.decode()), delimiter="\t", quoting=csv.QUOTE_NONE
            )
            if row["level"] == "5" and row["text"].strip()
        ]
        lines: dict[tuple[str, ...], list[str]] = {}
        for word in words:
            key = tuple(word[name] for name in ("block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append(word["text"])
        scores = [float(word["conf"]) for word in words]
        if any(not 0 <= score <= 100 for score in scores):
            raise ValueError("invalid word score")
        text = "\n".join(" ".join(line) for line in lines.values())
        return {
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "words": len(words),
            "mean_confidence": sum(scores) / len(scores) if scores else 0,
            "low_confidence_words": sum(score < 60 for score in scores),
        }
    except (KeyError, ValueError, UnicodeError, TypeError) as exc:
        raise OcrQualityError("Malformed OCR word output") from exc


def recognize_image(
    body: bytes, cache_root: Path, heartbeat: Callable[[], None] | None = None
) -> dict[str, Any]:
    if len(body) > 12_000_000:
        raise OcrQualityError("OCR image exceeds its size limit")
    context = engine_context()
    digest = hashlib.sha256(body).hexdigest()
    key = hashlib.sha256((digest + json.dumps(context, sort_keys=True)).encode()).hexdigest()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / f"{key}.json"
    if cache.exists():
        if cache.stat().st_size > 2_000_000:
            raise OcrQualityError("OCR cache exceeds its size limit")
        result: dict[str, Any] = json.loads(cache.read_text(encoding="utf-8"))
        if result["input_sha256"] != digest or result["context"] != context:
            raise OcrQualityError("OCR cache identity mismatch")
        layouts = checked_layouts(result["layouts"])
        if result["readable"] and len(layouts) == len(result["layouts"]):
            return result
    try:
        with Image.open(io.BytesIO(body)) as original:
            if (
                original.format not in {"JPEG", "PNG"}
                or original.width * original.height > 20_000_000
            ):
                raise OcrQualityError("OCR image format or dimensions are unsupported")
            original.load()
            image = original.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise OcrQualityError("OCR image cannot be completely decoded") from exc
    # An empty OCR string is NOT a blank page. Only pixel-uniform images pass as blank.
    blank = blank_image(image)
    layouts = []
    if not blank:
        for prior_version in ("local-ocr-v3", "local-ocr-v2"):
            prior_context = dict(context)
            prior_context["version"] = prior_version
            if prior_version == "local-ocr-v2":
                prior_context.pop("best_sha256", None)
            prior_key = hashlib.sha256(
                (digest + json.dumps(prior_context, sort_keys=True)).encode()
            ).hexdigest()
            prior_path = cache_root / f"{prior_key}.json"
            if prior_path.is_file() and prior_path.stat().st_size <= 2_000_000:
                prior = json.loads(prior_path.read_text(encoding="utf-8"))
                if prior["context"] != prior_context or prior["input_sha256"] != digest:
                    raise OcrQualityError("Prior recognition context mismatch")
                layouts = checked_layouts(prior["layouts"])
                if layouts:
                    break
        with tempfile.TemporaryDirectory(prefix="wtf-ocr-") as directory:
            path = Path(directory) / "page.png"
            image.save(path)
            for psm in (3, 11):
                if heartbeat:
                    heartbeat()
                if any(item["psm"] == psm and item["language"] == "eng" for item in layouts):
                    continue
                item = (
                    legacy_layout(digest, context, cache_root)
                    if psm == 3 and "english_sha256" in context
                    else None
                )
                if item is None:
                    item = parse_tsv(
                        _run(
                            [
                                "/usr/bin/tesseract",
                                str(path),
                                "stdout",
                                "-l",
                                "eng",
                                "--psm",
                                str(psm),
                                "tsv",
                            ]
                        )
                    )
                layouts.append({**item, "psm": psm, "language": "eng"})
            if not readable_layouts(layouts):
                if heartbeat:
                    heartbeat()
                item = parse_tsv(
                    _run(
                        [
                            "/usr/bin/tesseract",
                            str(path),
                            "stdout",
                            "-l",
                            "eng+chi_tra",
                            "--psm",
                            "11",
                            "tsv",
                        ]
                    )
                )
                layouts.append({**item, "psm": 11, "language": "eng+chi_tra"})
            if not readable_layouts(layouts):
                factor = min(2.0, (20_000_000 / (image.width * image.height)) ** 0.5)
                enlarged = image.resize(
                    (int(image.width * factor), int(image.height * factor)),
                    Image.Resampling.LANCZOS,
                )
                enlarged.save(path, dpi=(300, 300))
                for language in ("eng", "eng+chi_tra"):
                    for psm in (3, 11):
                        if heartbeat:
                            heartbeat()
                        arguments = [
                            "/usr/bin/tesseract",
                            str(path),
                            "stdout",
                            "-l",
                            language,
                            "--psm",
                            str(psm),
                        ]
                        if context.get("best_sha256"):
                            arguments.extend(["--tessdata-dir", "/opt/ocr-best"])
                        item = parse_tsv(
                            _run([*arguments, "-c", "tessedit_create_tsv=1"], timeout=90)
                        )
                        layouts.append(
                            {
                                **item,
                                "psm": psm,
                                "language": language,
                                "scale": factor,
                                "model": "best" if context.get("best_sha256") else "fast",
                            }
                        )
    readable = blank or readable_layouts(layouts)
    result = {
        "input_sha256": digest,
        "context": context,
        "blank": blank,
        "readable": readable,
        "layouts": layouts,
    }
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps(result), encoding="utf-8")
    replace_atomic(temporary, cache)
    return result


def render_pdf(raw: bytes, heartbeat: Callable[[], None] | None = None) -> list[bytes]:
    """Rasterize complete bounded PDFs; native text extraction remains the first choice."""
    if len(raw) > 10_000_000 or not raw.startswith(b"%PDF-"):
        raise PayloadError("PDF cannot enter OCR: invalid signature or byte bound")
    with tempfile.TemporaryDirectory(prefix="wtf-pdf-ocr-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(raw)
        info = _run(["/usr/bin/pdfinfo", str(path)]).decode()
        count = re.search(r"(?m)^Pages:\s+(\d+)\s*$", info)
        if not count or not 1 <= int(count[1]) <= 150 or re.search(r"Encrypted:\s+yes", info):
            raise OcrQualityError("PDF is encrypted or exceeds the OCR page bound")
        pages = []
        for number in range(1, int(count[1]) + 1):
            if heartbeat:
                heartbeat()
            prefix = Path(directory) / "page"
            _run(
                [
                    "/usr/bin/pdftoppm",
                    "-f",
                    str(number),
                    "-l",
                    str(number),
                    "-singlefile",
                    "-scale-to",
                    "2400",
                    "-png",
                    str(path),
                    str(prefix),
                ],
                timeout=45,
            )
            image_path = prefix.with_suffix(".png")
            if image_path.stat().st_size > 12_000_000:
                raise OcrQualityError("Rendered PDF page exceeds the image byte bound")
            pages.append(image_path.read_bytes())
            if sum(map(len, pages)) > 100_000_000:
                raise OcrQualityError("Rendered document exceeds the aggregate image bound")
        return pages
