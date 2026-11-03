"""Offline resolution trials on the remaining low-quality source pages."""

import csv
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

root = Path(sys.argv[1])
manifest = json.loads((root / "capture.json").read_text())
pages = next(d for d in manifest["documents"] if d["ticker"] == "WYNN")["pages"]
for page in pages:
    if page["page_number"] not in {1, 139}:
        continue
    with Image.open(root / page["file"]) as original:
        image = original.convert("RGB")
    if page["page_number"] == 2:
        difference = ImageChops.difference(image, Image.new("RGB", image.size, "white"))
        box = difference.convert("L").point(lambda x: 255 if x > 30 else 0).getbbox()
        cropped = image.crop((box[0] + 4, box[1] + 4, box[2] - 4, box[3] - 4))
        print(
            json.dumps({"page": 2, "bbox": box, "stddev": ImageStat.Stat(cropped).stddev}),
            flush=True,
        )
        continue
    with tempfile.TemporaryDirectory(prefix="wtf-resolution-") as folder:
        path = Path(folder) / "page.png"
        image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS).save(
            path, dpi=(300, 300)
        )
        for language in ("eng", "eng+chi_tra"):
            started = time.perf_counter()
            result = subprocess.run(  # noqa: S603 - fixed local binary and retained source
                [
                    "/usr/bin/tesseract",
                    str(path),
                    "stdout",
                    "-l",
                    language,
                    "--psm",
                    "11",
                    "--tessdata-dir",
                    "/opt/ocr-best",
                    "-c",
                    "tessedit_create_tsv=1",
                ],
                check=True,
                capture_output=True,
                timeout=90,
            )
            words = [
                r
                for r in csv.DictReader(
                    io.StringIO(result.stdout.decode()), delimiter="\t", quoting=csv.QUOTE_NONE
                )
                if r["level"] == "5" and r["text"].strip()
            ]
            scores = [float(r["conf"]) for r in words]
            print(
                json.dumps(
                    {
                        "page": page["page_number"],
                        "language": language,
                        "seconds": time.perf_counter() - started,
                        "words": len(words),
                        "confidence": sum(scores) / len(scores) if scores else 0,
                        "low_confidence": sum(s < 60 for s in scores),
                        "invalid_scores": [s for s in scores if not 0 <= s <= 100],
                        "text": " ".join(r["text"] for r in words)[:300],
                    }
                ),
                flush=True,
            )
