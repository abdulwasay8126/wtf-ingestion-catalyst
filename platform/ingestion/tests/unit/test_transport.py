from __future__ import annotations

import gzip

import httpx
import pytest

from wtf_ingestion.adapters.base import (
    FetchPolicy,
    PayloadError,
    PayloadTooLargeError,
    SafeHttpClient,
    SourcePermissionError,
    TransportError,
)


def policy() -> FetchPolicy:
    return FetchPolicy(
        frozenset({"data.sec.gov"}),
        "Fixture Test",
        max_response_bytes=10,
        content_types=("application/json",),
        min_interval_seconds=0,
    )


@pytest.mark.parametrize("payload,too_large", [(b"{}", False), (b"a" * 10000, True)])
def test_compression_keeps_decoded_size_guard(payload: bytes, too_large: bool) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=gzip.compress(payload),
        )

    client = SafeHttpClient(policy(), transport=httpx.MockTransport(respond))
    try:
        if too_large:
            with pytest.raises(PayloadTooLargeError):
                client.get("https://data.sec.gov/test")
        else:
            assert client.get("https://data.sec.gov/test")[0] == payload
    finally:
        client.close()


@pytest.mark.parametrize("size,too_large", [(10_240_918, False), (12_000_001, True)])
def test_bounded_large_html_response(size: int, too_large: bool) -> None:
    bounded = FetchPolicy(
        frozenset({"data.sec.gov"}),
        "Offline test",
        max_response_bytes=12_000_000,
        min_interval_seconds=0,
        content_types=("text/html",),
    )
    client = SafeHttpClient(
        bounded,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"a" * size,
            )
        ),
    )
    try:
        if too_large:
            with pytest.raises(PayloadTooLargeError):
                client.get("https://data.sec.gov/test")
        else:
            assert len(client.get("https://data.sec.gov/test")[0]) == size
    finally:
        client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://data.sec.gov/x",
        "https://evil.invalid/x",
        "https://fixture-user:fixture-password@data.sec.gov/x",
        "https://data.sec.gov/x?api_key=example-test-key",
    ],
)
def test_unsafe_destinations_fail_before_request(url: str) -> None:
    client = SafeHttpClient(
        policy(), transport=httpx.MockTransport(lambda request: pytest.fail("no request permitted"))
    )
    try:
        with pytest.raises(SourcePermissionError):
            client.get(url)
    finally:
        client.close()


@pytest.mark.parametrize(
    "response,error",
    [
        (
            httpx.Response(302, headers={"location": "https://data.sec.gov/new"}),
            SourcePermissionError,
        ),
        (httpx.Response(200, headers={"content-type": "text/html"}, content=b"bad"), PayloadError),
        (
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"a" * 11),
            PayloadError,
        ),
        (httpx.Response(403), TransportError),
        (httpx.Response(304), SourcePermissionError),
    ],
)
def test_transport_rejects_unusable_response(
    response: httpx.Response, error: type[Exception]
) -> None:
    client = SafeHttpClient(policy(), transport=httpx.MockTransport(lambda request: response))
    try:
        with pytest.raises(error):
            client.get("https://data.sec.gov/test")
    finally:
        client.close()


def test_retry_is_bounded_audited_and_headers_are_safe() -> None:
    attempts = []
    sleeps = []

    def respond(request: httpx.Request) -> httpx.Response:
        if len(attempts) < 2:
            return httpx.Response(429, headers={"retry-after": "999"})
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "set-cookie": "sensitive", "etag": "v1"},
            content=b"{}",
        )

    client = SafeHttpClient(
        policy(),
        transport=httpx.MockTransport(respond),
        sleep=sleeps.append,
        audit=lambda attempt, status: attempts.append((attempt, status)),
    )
    try:
        raw, headers = client.get("https://data.sec.gov/test")
    finally:
        client.close()
    assert raw == b"{}" and "set-cookie" not in headers
    assert attempts == [(1, "4xx"), (2, "4xx"), (3, "2xx")]
    assert sleeps == [20, 20]
