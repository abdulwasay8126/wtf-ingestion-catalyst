from __future__ import annotations

import pytest
from pydantic import ValidationError

from wtf_ingestion.config import Settings


@pytest.mark.parametrize(
    "identity",
    [
        "World Trade Factory",
        "",
        "example@example.com",
        "Organization contact@example.com\r\nX-Header: injected",
    ],
)
def test_sec_identity_requires_company_and_safe_email(identity: str) -> None:
    with pytest.raises(ValidationError):
        Settings(sec_user_agent=identity)


def test_complete_runtime_identity_still_requires_enable_flag() -> None:
    settings = Settings(sec_user_agent="Fixture Organization fixture@example.invalid")
    with pytest.raises(RuntimeError, match="disabled"):
        settings.assert_live_sec_ready()


@pytest.mark.parametrize("rate", [0, 0.49, 8.001, 10, float("inf"), float("nan")])
def test_sec_rate_keeps_headroom_below_aggregate_ceiling(rate: float) -> None:
    with pytest.raises(ValidationError):
        Settings(sec_requests_per_second=rate)


def test_sec_rate_default_and_polite_override() -> None:
    assert Settings().sec_requests_per_second == 8
    assert Settings(sec_requests_per_second=2).sec_requests_per_second == 2
