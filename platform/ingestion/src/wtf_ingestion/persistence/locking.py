from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

_LOCAL_LOCK = RLock()


class PipelineBusyError(RuntimeError):
    """The pipeline lease is occupied; callers may defer without renewing evidence."""


@contextmanager
def pipeline_lock(session: Session, key: int = 8621901) -> Iterator[None]:
    """Keep a dedicated PG connection checked out across ledger commits."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        with _LOCAL_LOCK:
            yield
        return
    engine = bind.engine if isinstance(bind, Connection) else bind
    if not isinstance(engine, Engine):
        raise RuntimeError("pipeline requires an engine-bound session")
    with engine.connect() as connection:
        locked = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        connection.commit()
        if not locked:
            raise PipelineBusyError("another collection or publication is already running")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()
