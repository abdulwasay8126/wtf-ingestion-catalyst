"""Offline bounded OCR. Text is review evidence, never event facts or a publication."""

import csv
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def validate_document(document):
    numbers = [page["page_number"] for page in document["pages"]]
    total = document["total_pages"]
    if not 1 <= total <= 150 or len(numbers) != len(set(numbers)):
        raise ValueError("invalid or duplicate OCR pages")
    if any(not 1 <= number <= total for number in numbers):
        raise ValueError("OCR page leaves document bounds")
    if document["complete_capture"] and numbers != list(range(1, total + 1)):
        raise ValueError("complete OCR document has missing or unordered pages")


def review_html(source, output, report, destination):
    """Offline, escaped side-by-side review; neither a verdict nor an ingestion interface."""
    sections = []
    for page in report["pages"]:
        result = json.loads((output / page["cache_file"]).read_text(encoding="utf-8"))
        relative = os.path.relpath(source.parent / page["image_file"], output).replace("\\", "/")
        text = html.escape(result["text"])
        confidence = page["mean_word_confidence"]
        label = "not available" if confidence is None else f"{confidence:.2f}"
        sections.append(
            f'<details id="{html.escape(page["ticker"])}-{page["page_number"]}"><summary>'
            f"{html.escape(page['ticker'])} · page {page['page_number']}/{page['total_pages']}"
            f" · {page['word_count']} words · mean confidence {label}</summary>"
            f"<p>Observed {html.escape(page['retrieved_at'])} · "
            f'<a href="{html.escape(page["image_url"], quote=True)}" rel="noreferrer">'
            "Official image</a>"
            f" · SHA-256 {html.escape(result['input_sha256'])}</p>"
            f'<div class="page"><img loading="lazy" src="{html.escape(relative, quote=True)}" '
            f'alt="Source page {page["page_number"]}"><pre>{text}</pre></div></details>'
        )
    title = "Complete-document OCR evidence review"
    destination.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "img-src 'self'; style-src 'unsafe-inline'\">"
        f"<title>{title}</title><style>body{{font:16px system-ui;margin:2rem;color:#173042;"
        "background:#f5f7fa}details{background:white;padding:1rem;margin:1rem 0;"
        "border:1px solid #ccd}summary{cursor:pointer;font-weight:600}"
        ".page{display:grid;grid-template-columns:1fr 1fr;gap:1rem}"
        "img{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px monospace}"
        "@media(max-width:800px){.page{grid-template-columns:1fr}}</style>"
        f"<h1>{title}</h1><p>REVIEW REQUIRED · Not accepted event data.</p>"
        f"<p>{len(report['pages'])} pages. Complete capture: {report['complete_capture']}. "
        "Confidence is an engine diagnostic, not an accuracy guarantee. Inspect every source image "
        "against its text, including covers, tables, blank pages and dates; a keyword search or "
        "no extracted match does not prove absence. Record uncertain text for review; do not guess "
        "or approve the full document from samples.</p>" + "".join(sections) + "</html>",
        encoding="utf-8",
    )


def main():
    from PIL import Image

    started = time.perf_counter()
    source = Path(sys.argv[1]).resolve()
    manifest = json.loads(source.read_text())
    if manifest["state"] != "CAPTURED_REVIEW_ONLY":
        raise ValueError("capture did not finish successfully")
    output = Path(sys.argv[2]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    engine = (
        subprocess.run(
            ["/usr/bin/tesseract", "--version"], check=True, capture_output=True, timeout=10
        )
        .stdout.decode()
        .splitlines()[0]
    )
    traineddata = Path("/usr/share/tesseract-ocr/5/tessdata/eng.traineddata")
    context = {
        "engine": engine,
        "language_sha256": hashlib.sha256(traineddata.read_bytes()).hexdigest(),
        "language": "eng",
        "page_segmentation": 3,
        "program_version": "local-ocr-trial-v1",
    }
    context_bytes = json.dumps(context, sort_keys=True).encode()
    report = {
        "purpose": "REVIEW ONLY; not feed accepted",
        "engine": context,
        "pages": [],
        "capture_directory": source.parent.name,
        "capture_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    for document in manifest["documents"]:
        validate_document(document)
        for page in document["pages"]:
            name = page["file"]
            if name != page["sha256"] + ".bin" or not re.fullmatch(r"[a-f0-9]{64}", page["sha256"]):
                raise ValueError("invalid image reference")
            image_path = source.parent / name
            body = image_path.read_bytes()
            if len(body) > 12_000_000 or hashlib.sha256(body).hexdigest() != page["sha256"]:
                raise ValueError("image checksum/size mismatch")
            with Image.open(io.BytesIO(body)) as image:
                if image.format not in {"JPEG", "PNG"} or image.width * image.height > 20_000_000:
                    raise ValueError("unsupported or oversized image")
                image.verify()
            cache_key = hashlib.sha256(page["sha256"].encode() + context_bytes).hexdigest()
            cache = output / (cache_key + ".json")
            page_started = time.perf_counter()
            reused = cache.exists()
            if reused:
                result = json.loads(cache.read_text())
                if result["input_sha256"] != page["sha256"] or result["context"] != context:
                    raise ValueError("OCR cache context mismatch")
                if hashlib.sha256(result["text"].encode()).hexdigest() != result["text_sha256"]:
                    raise ValueError("OCR cache text checksum mismatch")
            else:
                # Fixed binary, verified checksum basename, no shell, offline bounded container.
                process = subprocess.run(  # noqa: S603
                    [
                        "/usr/bin/tesseract",
                        str(image_path),
                        "stdout",
                        "-l",
                        "eng",
                        "--psm",
                        "3",
                        "tsv",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                if len(process.stdout) > 2_000_000:
                    raise ValueError("OCR output exceeds trial bound")
                rows = list(
                    csv.DictReader(io.StringIO(process.stdout.decode("utf-8")), delimiter="\t")
                )
                words = [row for row in rows if row["level"] == "5" and row["text"].strip()]
                lines = {}
                for row in words:
                    key = tuple(row[field] for field in ("block_num", "par_num", "line_num"))
                    lines.setdefault(key, []).append(row["text"])
                text = "\n".join(" ".join(line) for line in lines.values())
                scores = [float(row["conf"]) for row in words]
                result = {
                    "input_sha256": page["sha256"],
                    "context": context,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "word_count": len(words),
                    "mean_word_confidence": sum(scores) / len(scores) if scores else None,
                    "low_confidence_words": sum(score < 80 for score in scores),
                    "review_required": True,
                    "recognition_seconds": time.perf_counter() - page_started,
                }
                with cache.open("x", encoding="utf-8") as handle:
                    json.dump(result, handle, indent=2)
            report["pages"].append(
                {
                    "ticker": document["ticker"],
                    "page_number": page["page_number"],
                    "total_pages": document["total_pages"],
                    "image_url": page["url"],
                    "image_file": name,
                    "retrieved_at": page["retrieved_at"],
                    "cache_file": cache.name,
                    "cache_reused": reused,
                    "elapsed_seconds": time.perf_counter() - page_started,
                    "word_count": result["word_count"],
                    "mean_word_confidence": result["mean_word_confidence"],
                }
            )
            if len(report["pages"]) % 10 == 0:
                print(json.dumps({"processed_pages": len(report["pages"])}), flush=True)
    report["elapsed_seconds"] = time.perf_counter() - started
    report["complete_capture"] = all(doc["complete_capture"] for doc in manifest["documents"])
    identity = str(time.time_ns())
    review = output / ("review-" + identity + ".html")
    report["review_file"] = review.name
    review_html(source, output, report, review)
    with (output / ("report-" + identity + ".json")).open("x") as handle:
        json.dump(report, handle, indent=2)
    if report["complete_capture"]:
        pointer = {"report_file": "report-" + identity + ".json"}
        temporary = output / "latest-complete.tmp"
        temporary.write_text(json.dumps(pointer), encoding="utf-8")
        temporary.replace(output / "latest-complete.json")
    print(
        json.dumps(
            {
                "pages": len(report["pages"]),
                "elapsed_seconds": report["elapsed_seconds"],
                "complete_capture": report["complete_capture"],
                "review_file": review.name,
                "reused_pages": sum(page["cache_reused"] for page in report["pages"]),
            }
        )
    )


if __name__ == "__main__":
    main()
