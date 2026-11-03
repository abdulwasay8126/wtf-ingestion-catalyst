"""Revision-local schemas and preservation checks in disposable PostgreSQL schemas."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from wtf_ingestion.config import get_settings
from wtf_ingestion.persistence.database import Base
from wtf_ingestion.persistence.models import Source
from wtf_ingestion.registry import SOURCE_RECORDS

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def migration_db(monkeypatch):  # type: ignore[no-untyped-def]
    url = os.environ.get("WTF_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set WTF_MIGRATION_TEST_DATABASE_URL to an isolated PostgreSQL test database")
    admin = sa.create_engine(url)
    assert admin.dialect.name == "postgresql"
    schema = "pr_review_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    scoped = admin.url.update_query_dict({"options": f"-csearch_path={schema}"})
    monkeypatch.setenv("WTF_DATABASE_URL", scoped.render_as_string(hide_password=False))
    get_settings.cache_clear()
    engine = sa.create_engine(scoped)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    try:
        yield engine, config
    finally:
        engine.dispose()
        get_settings.cache_clear()
        # Only this fixture's random schema is removed, never a database or volume.
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def columns(engine, table):  # type: ignore[no-untyped-def]
    return {item["name"] for item in sa.inspect(engine).get_columns(table)}


def test_fresh_head_ignores_future_orm_columns(migration_db):  # type: ignore[no-untyped-def]
    engine, config = migration_db
    tables = (
        "refresh_jobs",
        "refresh_workers",
        "sec_company_checkpoints",
        "sec_document_parses",
        "sec_metadata_checks",
    )
    additions = [sa.Column("future_orm_only", sa.Text()) for _ in tables]
    for table, column in zip(tables, additions, strict=True):
        Base.metadata.tables[table].append_column(column)
    try:
        command.upgrade(config, "head")
        for table in tables:
            assert "future_orm_only" not in columns(engine, table)
            assert columns(engine, table) == set(Base.metadata.tables[table].columns.keys()) - {
                "future_orm_only"
            }
    finally:
        for table, column in zip(tables, additions, strict=True):
            Base.metadata.tables[table]._columns.remove(column)
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20261004_0010"
        for view in ("events_feed_horizon", "events_feed_current"):
            table = sa.Table(view, sa.MetaData(), autoload_with=conn)
            assert list(conn.execute(sa.select(table))) == []
    inspection = sa.inspect(engine)
    assert any(
        item["name"] == "uq_sec_checkpoint_run_company"
        for item in inspection.get_unique_constraints("sec_company_checkpoints")
    )
    assert any(
        item["name"] == "ix_sec_checkpoint_resume"
        for item in inspection.get_indexes("sec_company_checkpoints")
    )


@pytest.mark.parametrize("legacy_columns", [False, True], ids=["revision-local", "old-orm-drift"])
def test_upgrade_preserves_rows_views_and_triggers(migration_db, legacy_columns):  # type: ignore[no-untyped-def]
    engine, config = migration_db
    command.upgrade(config, "20261001_0005")
    with Session(engine) as session:
        record = SOURCE_RECORDS[0]
        values = record.model_dump(mode="python")
        values["base_url"] = str(record.base_url)
        session.add(Source(**values))
        session.commit()
    now = datetime.now(UTC)
    ids = {name: uuid4() for name in ("company", "universe", "action", "run", "raw", "checkpoint")}
    with engine.begin() as conn:
        source_id = conn.scalar(sa.text("SELECT id FROM sources WHERE key='sec_edgar'"))

        def insert(table, **values):  # type: ignore[no-untyped-def]
            reflected = sa.Table(table, sa.MetaData(), autoload_with=conn)
            conn.execute(reflected.insert().values(**values))

        insert(
            "companies",
            id=ids["company"],
            ticker="TEST",
            exchange="TEST",
            company_name="Synthetic migration issuer",
            cik="0000000001",
            active=True,
            aliases=[],
        )
        insert(
            "universe_versions",
            id=ids["universe"],
            name="Synthetic scope",
            version="test-v1",
            effective_from=now.date(),
            assumption=True,
        )
        insert(
            "operator_actions",
            id=ids["action"],
            action_type="test",
            actor="fixture",
            target_id="test",
            reason="migration preservation",
            idempotency_key="fixture",
        )
        insert(
            "collection_runs",
            id=ids["run"],
            source_id=source_id,
            trigger="test",
            evidence_mode="fixture",
            state="succeeded",
            stage="persisted",
            started_at=now,
            received_count=0,
            accepted_count=0,
            rejected_count=0,
            duplicate_count=0,
            inserted_count=0,
            unchanged_count=0,
            revised_count=0,
            anomaly_flags=[],
        )
        insert(
            "raw_objects",
            id=ids["raw"],
            run_id=ids["run"],
            sha256="a" * 64,
            byte_size=2,
            content_type="application/json",
            source_url="https://example.test/fixture",
            retrieved_at=now,
            parser_version="fixture-v1",
        )
        baseline = {
            table: list(conn.execute(sa.text(f"SELECT row_to_json(t) FROM {table} t")))  # noqa: S608 - fixed fixture tables
            for table in ("sources", "companies", "operator_actions", "raw_objects")
        }
        view = conn.scalar(sa.text("SELECT pg_get_viewdef('events_feed_current'::regclass)"))
    command.upgrade(config, "20261001_0006")
    assert "metadata_watch" not in columns(engine, "refresh_workers")
    with engine.begin() as conn:
        conn.execute(
            sa.text("""INSERT INTO refresh_workers (key, heartbeat_at)
            VALUES ('sentinel', :now)"""),
            {"now": now},
        )
        if legacy_columns:
            conn.execute(
                sa.text("ALTER TABLE refresh_workers ADD metadata_watch JSON NOT NULL DEFAULT '{}'")
            )
            workers = sa.Table("refresh_workers", sa.MetaData(), autoload_with=conn)
            conn.execute(workers.update().values(metadata_watch={"saved": True}))
    command.upgrade(config, "20261001_0008")
    assert "reused_parses" not in columns(engine, "sec_company_checkpoints")
    with engine.begin() as conn:
        if legacy_columns:
            conn.execute(
                sa.text(
                    "ALTER TABLE sec_company_checkpoints ADD reused_parses "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )
        checkpoint = sa.Table("sec_company_checkpoints", sa.MetaData(), autoload_with=conn)
        values = dict(
            id=ids["checkpoint"],
            run_id=ids["run"],
            universe_version_id=ids["universe"],
            company_id=ids["company"],
            parser_version="fixture-v1",
            config_key="b" * 64,
            state="validated",
            checked_at=now,
            saved_at=now,
            payload={"saved": True},
            network_requests=1,
            reused_objects=2,
        )
        if legacy_columns:
            values["reused_parses"] = 7
        conn.execute(checkpoint.insert().values(**values))
    command.upgrade(config, "head")
    with engine.connect() as conn:
        for table, rows in baseline.items():
            query = sa.text(f"SELECT row_to_json(t) FROM {table} t")  # noqa: S608 - fixed fixture tables
            assert list(conn.execute(query)) == rows
        assert (
            conn.scalar(sa.text("SELECT pg_get_viewdef('events_feed_horizon'::regclass)")) == view
        )
        assert list(conn.execute(sa.text("SELECT * FROM events_feed_current"))) == []
        assert conn.scalar(sa.text("SELECT reused_parses FROM sec_company_checkpoints")) == (
            7 if legacy_columns else 0
        )
        assert conn.scalar(sa.text("SELECT payload FROM sec_company_checkpoints")) == {
            "saved": True
        }
        assert conn.scalar(sa.text("SELECT metadata_watch FROM refresh_workers")) == (
            {"saved": True} if legacy_columns else {}
        )
        triggers = dict(
            conn.execute(
                sa.text("""SELECT c.relname, t.tgname FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND NOT t.tgisinternal
                AND t.tgname LIKE 'immutable_%'""")
            ).all()
        )
        for table in (
            "raw_objects",
            "event_versions",
            "source_assertions",
            "sec_company_checkpoints",
            "sec_document_parses",
            "sec_metadata_checks",
        ):
            assert triggers[table] == (
                "immutable_evidence"
                if table in ("raw_objects", "event_versions", "source_assertions")
                else "immutable_" + table
            )
    for table in ("raw_objects", "sec_company_checkpoints"):
        reflected = sa.Table(table, sa.MetaData(), autoload_with=engine)
        for operation in (reflected.update().values(id=reflected.c.id), reflected.delete()):
            with pytest.raises(sa.exc.DBAPIError, match="append-only"):
                with engine.begin() as conn:
                    conn.execute(operation)
    # Round-trip only newly created cache/queue tables; accepted raw evidence survives.
    command.downgrade(config, "20261001_0005")
    assert "refresh_jobs" not in sa.inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert (
            conn.scalar(sa.text("SELECT pg_get_viewdef('events_feed_current'::regclass)")) == view
        )
        assert (
            list(conn.execute(sa.text("SELECT row_to_json(t) FROM raw_objects t")))
            == baseline["raw_objects"]
        )
    command.upgrade(config, "head")
