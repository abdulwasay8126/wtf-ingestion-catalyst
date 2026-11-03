from __future__ import annotations

from itertools import pairwise

import httpx
import pytest

from wtf_ingestion.adapters import base
from wtf_ingestion.adapters.base import (
    AccessBlockedError,
    FetchPolicy,
    SafeHttpClient,
    ThrottledError,
)
from wtf_ingestion.config import Settings


@pytest.mark.parametrize("status,error", [(403, AccessBlockedError), (429, ThrottledError)])
def test_sec_access_notice_stops_immediately(status: int, error: type[Exception]) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status)

    client = SafeHttpClient(
        FetchPolicy(
            frozenset({"data.sec.gov"}),
            "Fixture Organization fixture@example.invalid",
            stop_on_throttle=True,
        ),
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(error):
            client.get("https://data.sec.gov/test")
    finally:
        client.close()
    assert len(calls) == 1


def test_serial_rate_is_shared_across_sec_hosts(monkeypatch):  # type: ignore[no-untyped-def]
    clock = [100.0]
    starts = []

    def sleep(delay: float) -> None:
        clock[0] += delay

    def respond(request: httpx.Request) -> httpx.Response:
        starts.append(clock[0])
        return httpx.Response(200, content=b"{}")

    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    client = SafeHttpClient(
        FetchPolicy(
            frozenset({"data.sec.gov", "www.sec.gov"}),
            "Offline rate test",
            min_interval_seconds=1 / Settings().sec_requests_per_second,
            stop_on_throttle=True,
        ),
        transport=httpx.MockTransport(respond),
        sleep=sleep,
    )
    try:
        for index in range(40):
            host = "data.sec.gov" if index % 2 else "www.sec.gov"
            client.get(f"https://{host}/fixture/{index}")
    finally:
        client.close()
    assert all(b - a >= 0.125 for a, b in pairwise(starts))
    assert max(sum(start <= value < start + 1 for value in starts) for start in starts) == 8
