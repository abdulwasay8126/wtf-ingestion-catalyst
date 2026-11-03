from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from wtf_ingestion.persistence.database import Base
from wtf_ingestion.registry import seed_registry


@pytest.fixture(autouse=True)
def fixture_clock():  # type: ignore[no-untyped-def]
    with freeze_time("2026-10-01T12:00:00Z", tick=True):
        yield


@pytest.fixture
def db_session(tmp_path: Path) -> Generator[Session, None, None]:
    url = os.environ.get("WTF_TEST_DATABASE_URL")
    engine = create_engine(url or f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    if url is None:
        Base.metadata.create_all(engine)
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session = factory()
    seed_registry(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def fixture_root() -> Path:
    return Path(__file__).parent / "fixtures"
