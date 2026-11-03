from __future__ import annotations

import hashlib

import httpx
import pytest

from wtf_ingestion.adapters.base import CachedResponse, FetchPolicy, PayloadError, SafeHttpClient


def test_validated_304_reuses_exact_checksum_verified_bytes() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"fixture-v1"'
        return httpx.Response(304, headers={"date": "Thu, 01 Oct 2026 12:00:00 GMT"})

    client = SafeHttpClient(
        FetchPolicy(frozenset({"data.sec.gov"}), "Fixture Test"),
        transport=httpx.MockTransport(respond),
    )
    url = "https://data.sec.gov/test"
    client.remember_validated(
        url, b"{}", httpx.Headers({"etag": '"fixture-v1"', "content-type": "application/json"})
    )
    try:
        body, headers = client.get(url)
    finally:
        client.close()
    assert body == b"{}" and headers["content-type"] == "application/json"


def test_tampered_cache_fails_before_network() -> None:
    client = SafeHttpClient(
        FetchPolicy(frozenset({"data.sec.gov"}), "Fixture Test"),
        transport=httpx.MockTransport(lambda request: pytest.fail("no request permitted")),
    )
    client.cache["https://data.sec.gov/test"] = CachedResponse(
        b"bad", httpx.Headers({"etag": "v1"}), hashlib.sha256(b"original").hexdigest()
    )
    try:
        with pytest.raises(PayloadError, match="checksum"):
            client.get("https://data.sec.gov/test")
    finally:
        client.close()
