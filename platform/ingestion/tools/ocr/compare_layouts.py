"""Offline comparison on retained low-confidence pages, never accepts event facts."""

import csv
import io
import json
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
report = json.loads(
    (
        root
        / "results"
        / json.loads((root / "results/latest-complete.json").read_text())["report_file"]
    ).read_text()
)
results = []
for page in report["pages"]:
    score = page["mean_word_confidence"]
    if score is not None and score >= 80:
        continue
    item = {
        "ticker": page["ticker"],
        "page": page["page_number"],
        "original_score": score,
        "layouts": [],
    }
    for psm in (6, 11):
        started = time.perf_counter()
        result = subprocess.run(  # noqa: S603 - fixed local binary, retained evidence only
            [
                "/usr/bin/tesseract",
                str(root / report["capture_directory"] / page["image_file"]),
                "stdout",
                "--psm",
                str(psm),
                "tsv",
            ],
            check=True,
            capture_output=True,
            timeout=45,
        )
        words = [
            row
            for row in csv.DictReader(io.StringIO(result.stdout.decode()), delimiter="\t")
            if row["level"] == "5" and row["text"].strip()
        ]
        scores = [float(w["conf"]) for w in words]
        alpha = [float(w["conf"]) for w in words if any(c.isalpha() for c in w["text"])]
        item["layouts"].append(
            {
                "psm": psm,
                "seconds": time.perf_counter() - started,
                "words": len(words),
                "confidence": sum(scores) / len(scores) if scores else None,
                "alpha_confidence": sum(alpha) / len(alpha) if alpha else None,
            }
        )
    results.append(item)
    print(json.dumps(item), flush=True)
