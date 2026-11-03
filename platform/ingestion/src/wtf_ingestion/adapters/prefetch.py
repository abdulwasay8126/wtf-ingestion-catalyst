"""Bounded metadata prefetch. Worker threads never touch SQLAlchemy or raw storage."""

import hashlib
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import LifoQueue

import httpx

from wtf_ingestion.adapters.base import (
    FetchPolicy,
    PayloadError,
    RequestGate,
    SafeHttpClient,
    SourceError,
)


@dataclass(frozen=True)
class CacheReference:
    path: Path
    sha256: str
    headers: dict[str, str]


@dataclass
class Result:
    body: bytes
    headers: httpx.Headers
    observed: datetime
    attempts: list[tuple[int, str]]
    error: SourceError | None = None


class MetadataPrefetch:
    def __init__(self, urls: list[str], policy: FetchPolicy, gate: RequestGate, workers: int):
        self.urls = iter(urls)
        self.workers = workers
        self.references: dict[str, CacheReference] = {}
        self.policy = policy
        self.gate = gate
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sec-metadata")
        self.clients: LifoQueue[SafeHttpClient] = LifoQueue(maxsize=workers)
        for _ in range(workers):
            self.clients.put(SafeHttpClient(policy, gate=gate))
        self.pending: dict[str, Future[Result]] = {}
        for _ in range(workers):
            self._submit()

    def prime(self, urls: list[str], references: dict[str, CacheReference]) -> None:
        if self.pending:
            raise RuntimeError("Previous prefetch batch was not consumed")
        self.urls = iter(dict.fromkeys(urls))
        self.references = references
        for _ in range(self.workers):
            self._submit()

    def _submit(self) -> None:
        url = next(self.urls, None)
        if url is not None:
            self.pending[url] = self.executor.submit(self._fetch, url)

    def _fetch(self, url: str) -> Result:
        attempts = []
        client = self.clients.get()
        client.audit = lambda attempt, status: attempts.append((attempt, status))
        try:
            try:
                reference = self.references.get(url)
                if reference:
                    try:
                        if reference.path.stat().st_size > self.policy.max_response_bytes:
                            raise PayloadError("Conditional cache exceeds its byte bound")
                        body = reference.path.read_bytes()
                    except OSError as exc:
                        raise PayloadError("Conditional cache is unavailable") from exc
                    if hashlib.sha256(body).hexdigest() != reference.sha256:
                        raise PayloadError("Conditional cache checksum mismatch")
                    client.remember_validated(url, body, httpx.Headers(reference.headers))
                body, headers = client.get(url)
                return Result(body, headers, datetime.now(UTC), attempts)
            except SourceError as exc:
                return Result(b"", httpx.Headers(), datetime.now(UTC), attempts, exc)
        finally:
            client.audit = None
            client.cache.pop(url, None)
            self.clients.put(client)

    def take(self, url: str) -> Result:
        future = self.pending.pop(url)
        result = future.result()
        self._submit()
        return result

    def drain(self) -> list[tuple[str, Result]]:
        results = [
            (url, future.result()) for url, future in self.pending.items() if not future.cancelled()
        ]
        self.pending = {}
        return results

    def close(self) -> list[tuple[str, Result]]:
        self.gate.stop()
        self.executor.shutdown(wait=True, cancel_futures=True)
        while not self.clients.empty():
            self.clients.get().close()
        # Even prefetched responses unused after an early abort belong in the HTTP ledger.
        return self.drain()
