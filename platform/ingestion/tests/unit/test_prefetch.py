import hashlib
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from wtf_ingestion.adapters import prefetch
from wtf_ingestion.adapters.base import AccessBlockedError, FetchPolicy, RequestGate, SafeHttpClient


def test_aggregate_gate_and_stop():
    gate = RequestGate(0.01)

    def request():
        gate.acquire()
        return time.monotonic()

    with ThreadPoolExecutor(max_workers=4) as workers:
        times = sorted(workers.map(lambda _: request(), range(8)))
    assert times[-1] - times[0] >= 0.065
    gate.stop()
    with pytest.raises(AccessBlockedError):
        gate.acquire()


def test_prefetch_preserves_failure_audit_and_bound(monkeypatch):
    policy = FetchPolicy(
        frozenset({"data.sec.gov"}), "Offline test", min_interval_seconds=0, stop_on_throttle=True
    )

    def factory(policy, **kwargs):
        return SafeHttpClient(
            policy, transport=httpx.MockTransport(lambda req: httpx.Response(429)), **kwargs
        )

    monkeypatch.setattr(prefetch, "SafeHttpClient", factory)
    worker = prefetch.MetadataPrefetch(
        ["https://data.sec.gov/a", "https://data.sec.gov/b"], policy, RequestGate(0), 1
    )
    assert len(worker.pending) == 1
    result = worker.take("https://data.sec.gov/a")
    assert result.error and result.error.code == "SOURCE_THROTTLED"
    assert result.attempts == [(1, "4xx")]
    assert len(worker.pending) <= 1
    worker.close()


@pytest.mark.parametrize("changed", [False, True])
def test_archive_conditional_cache_and_reusable_batches(tmp_path, monkeypatch, changed):
    created = []
    headers = []
    body = b"verified archive bytes"
    path = tmp_path / "cached.bin"
    path.write_bytes(body if not changed else b"changed")

    def respond(request):
        headers.append(request.headers.get("if-none-match"))
        return (
            httpx.Response(304)
            if request.headers.get("if-none-match")
            else httpx.Response(200, content=b"new")
        )

    def factory(policy, **kwargs):
        created.append(1)
        return SafeHttpClient(policy, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(prefetch, "SafeHttpClient", factory)
    policy = FetchPolicy(frozenset({"www.sec.gov"}), "Offline test", min_interval_seconds=0)
    worker = prefetch.MetadataPrefetch([], policy, RequestGate(0), 2)
    url = "https://www.sec.gov/a"
    worker.prime(
        [url],
        {url: prefetch.CacheReference(path, hashlib.sha256(body).hexdigest(), {"etag": '"v1"'})},
    )
    first = worker.take(url)
    if changed:
        assert first.error and first.error.code == "PAYLOAD_ERROR" and not headers
    else:
        assert first.body == body and first.attempts == [(1, "3xx")]
    worker.prime([url], {})
    assert worker.take(url).body == b"new"
    assert len(created) == 2 and not worker.pending
    worker.close()
