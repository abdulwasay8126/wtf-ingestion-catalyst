from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

SENSITIVE_QUERY = re.compile(
    r"(?i)(?:token|secret|password|api.?key|credential|signature|authorization)"
)


def validate_public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("provenance must be a public HTTPS URL without credentials")
    if parsed.port not in (None, 443) or parsed.fragment:
        raise ValueError("nonstandard ports and fragments are not accepted")
    if any(SENSITIVE_QUERY.search(key) for key, _ in parse_qsl(parsed.query)):
        raise ValueError("credential-bearing query strings are not accepted")
    return url
