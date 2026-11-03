"""Download pinned official Apache-2.0 Tesseract models for local fallback only."""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

COMMIT = "e12c65a915945e4c28e237a9b52bc4a8f39a0cec"
EXPECTED = {
    "eng": "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
    "chi_tra": "1aa60488574cafa69486d919284f079ca9b68fcc7f6ad8dc1ff1b318dfd97028",
}
root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
for language in ("eng", "chi_tra"):
    url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{COMMIT}/{language}.traineddata"
    request = urllib.request.Request(
        url, headers={"User-Agent": "World-Trade-Factory-local-OCR-build"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed HTTPS URL
        data = response.read(50_000_001)
    if not 1_000_000 <= len(data) <= 50_000_000:
        raise ValueError("Official OCR model exceeds its size bound")
    if hashlib.sha256(data).hexdigest() != EXPECTED[language]:
        raise ValueError("Official OCR model checksum mismatch")
    (root / f"{language}.traineddata").write_bytes(data)
    print(
        json.dumps(
            {
                "language": language,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "commit": COMMIT,
            }
        )
    )
