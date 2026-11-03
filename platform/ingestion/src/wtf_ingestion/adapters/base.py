from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from wtf_ingestion.safety import validate_public_url


class SourceError(RuntimeError):
    code = "SOURCE_ERROR"


class SourcePermissionError(SourceError):
    code = "SOURCE_NOT_APPROVED"


class TransportError(SourceError):
    code = "TRANSPORT_ERROR"


class AccessBlockedError(TransportError):
    code = "SOURCE_ACCESS_BLOCKED"


class ThrottledError(TransportError):
    code = "SOURCE_THROTTLED"


class PayloadError(SourceError):
    code = "PAYLOAD_ERROR"


class UnreadableDocumentError(PayloadError):
    code = "DOCUMENT_TEXT_UNREADABLE"


class PayloadTooLargeError(PayloadError):
    code = "PAYLOAD_TOO_LARGE"


class IncompleteScanError(PayloadError):
    code = "SEC_SCAN_INCOMPLETE"


@dataclass(frozen=True)
class FetchPolicy:
    allowed_hosts: frozenset[str]
    user_agent: str
    timeout_seconds: float = 20.0
    max_response_bytes: int = 10_000_000
    content_types: tuple[str, ...] = ()
    min_interval_seconds: float = 0.5
    max_attempts: int = 3
    stop_on_throttle: bool = False


@dataclass(frozen=True)
class CachedResponse:
    body: bytes
    headers: httpx.Headers
    sha256: str


class RequestGate:
    """One aggregate request-start budget, shared by bounded workers and retries."""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.last_request = 0.0

    def acquire(self) -> None:
        with self.lock:
            delay = max(0.0, self.interval - (time.monotonic() - self.last_request))
            if self.stopped.wait(delay):
                raise AccessBlockedError("Source request group stopped after access refusal")
            self.last_request = time.monotonic()

    def stop(self) -> None:
        self.stopped.set()


class SafeHttpClient:
    """Serial, bounded requests. Redirects and credential-bearing URLs fail closed."""

    def __init__(
        self,
        policy: FetchPolicy,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        audit: Callable[[int, str], None] | None = None,
        gate: RequestGate | None = None,
    ):
        self.policy = policy
        self.sleep = sleep
        self.audit = audit
        self.gate = gate
        self.last_request = 0.0
        self.cache: dict[str, CachedResponse] = {}
        self.client = httpx.Client(
            timeout=policy.timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": policy.user_agent, "Accept-Encoding": "gzip, deflate"},
        )

    def close(self) -> None:
        self.client.close()

    def remember_validated(self, url: str, body: bytes, headers: httpx.Headers) -> None:
        self.cache[url] = CachedResponse(body, headers, hashlib.sha256(body).hexdigest())

    def get(self, url: str) -> tuple[bytes, httpx.Headers]:
        try:
            validate_public_url(url)
        except ValueError as exc:
            raise SourcePermissionError("URL is not safe for public collection") from exc
        if urlparse(url).hostname not in self.policy.allowed_hosts:
            raise SourcePermissionError("URL is not an approved HTTPS host")
        cached = self.cache.get(url)
        conditional = {}
        if cached:
            if hashlib.sha256(cached.body).hexdigest() != cached.sha256:
                raise PayloadError("validated cache checksum mismatch")
            for original, header in (
                ("etag", "If-None-Match"),
                ("last-modified", "If-Modified-Since"),
            ):
                value = cached.headers.get(original)
                if value:
                    conditional[header] = value
        for attempt in range(1, self.policy.max_attempts + 1):
            if self.gate is not None:
                self.gate.acquire()
            else:
                delay = self.policy.min_interval_seconds - (time.monotonic() - self.last_request)
                if delay > 0:
                    self.sleep(delay)
                self.last_request = time.monotonic()
            try:
                with self.client.stream("GET", url, headers=conditional) as response:
                    status = response.status_code
                    if self.audit:
                        self.audit(attempt, f"{status // 100}xx")
                    if status == 403:
                        if self.gate:
                            self.gate.stop()
                        raise AccessBlockedError("source access blocked; operator review required")
                    if status == 429 and self.policy.stop_on_throttle:
                        if self.gate:
                            self.gate.stop()
                        raise ThrottledError("source throttled the request; collection stopped")
                    if status == 304 and cached and conditional:
                        if len(cached.body) > self.policy.max_response_bytes:
                            raise PayloadTooLargeError("cached payload exceeded size limit")
                        headers = httpx.Headers(cached.headers)
                        for name in ("date", "etag", "last-modified"):
                            if name in response.headers:
                                headers[name] = response.headers[name]
                        return cached.body, headers
                    if status in {429, 500, 502, 503, 504} and attempt < self.policy.max_attempts:
                        retry_after = response.headers.get("retry-after", "")
                        wait = float(retry_after) if retry_after.isdigit() else 2 ** (attempt - 1)
                        self.sleep(min(wait, 20.0))
                        continue
                    if 300 <= status < 400:
                        raise SourcePermissionError("redirect requires explicit endpoint approval")
                    if status != 200:
                        raise TransportError(f"unexpected HTTP status class {status // 100}xx")
                    content_type = response.headers.get("content-type", "").lower()
                    if self.policy.content_types and not any(
                        item in content_type for item in self.policy.content_types
                    ):
                        raise PayloadError("unexpected response content type")
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65_536):
                        data.extend(chunk)
                        if len(data) > self.policy.max_response_bytes:
                            raise PayloadTooLargeError("response exceeded configured size limit")
                    safe_headers = httpx.Headers(
                        {
                            key: response.headers[key]
                            for key in ("content-type", "etag", "last-modified", "date")
                            if key in response.headers
                        }
                    )
                    return bytes(data), safe_headers
            except httpx.HTTPError as exc:
                if self.audit:
                    self.audit(attempt, "network_error")
                if attempt == self.policy.max_attempts:
                    raise TransportError(f"request failed: {exc.__class__.__name__}") from exc
                self.sleep(float(2 ** (attempt - 1)))
        raise TransportError("request attempts exhausted")


def safe_fetch(url: str, policy: FetchPolicy) -> tuple[bytes, httpx.Headers]:
    client = SafeHttpClient(policy)
    try:
        return client.get(url)
    finally:
        client.close()
