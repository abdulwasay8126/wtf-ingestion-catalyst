from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".venv", ".git", "outputs", "raw", "backups", "__pycache__"}
TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".cjs",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".html",
    ".css",
    ".ps1",
    ".ini",
    ".txt",
    ".example",
}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "generic_assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"]{12,}['\"]"
    ),
}
ALLOW = {
    "Bearer hidden-token",
    '"api_key": "secret"',
    "WTF_SEC_USER_AGENT=",
}
# Reviewed public library keyword assigned to a numeric limit, not a literal credential.
ALLOW_HIGH_ENTROPY_TOKENS = {
    "array_based_stream_maximum_output_length=MAX_STREAM_BYTES",
    "maximum_declared_stream_length=MAX_PDF_BYTES",
}


def entropy(value: str) -> float:
    if not value:
        return 0.0
    frequencies = {char: value.count(char) / len(value) for char in set(value)}
    return -sum(probability * math.log2(probability) for probability in frequencies.values())


def main() -> int:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
            "Dockerfile",
            ".gitignore",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(allowed in line for allowed in ALLOW):
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{line_number}:{label}")
            for token in re.findall(r"[A-Za-z0-9+/=_-]{32,}", line):
                if token in ALLOW_HIGH_ENTROPY_TOKENS:
                    continue
                if entropy(token) >= 4.5 and not token.startswith(("https", "postgresql")):
                    findings.append(f"{path.relative_to(ROOT)}:{line_number}:high_entropy")
    if findings:
        print("Potential secrets found (values redacted):")
        print("\n".join(sorted(set(findings))))
        return 1
    print("Secret scan passed: no credential-shaped values found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
