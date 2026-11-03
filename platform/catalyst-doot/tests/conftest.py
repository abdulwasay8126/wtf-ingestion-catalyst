from pathlib import Path

import pytest
from freezegun import freeze_time


@pytest.fixture(autouse=True)
def fixture_clock():  # type: ignore[no-untyped-def]
    with freeze_time("2026-10-01T12:00:00Z", tick=True):
        yield


@pytest.fixture
def fixture_root() -> Path:
    return Path(__file__).parent / "fixtures"
