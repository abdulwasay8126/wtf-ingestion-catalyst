from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.orm import Session
from wtf_ingestion.adapters.alpaca import AlpacaClient
from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings, get_settings
from wtf_ingestion.earnings import earnings_data
from wtf_ingestion.earnings_evidence import pack_directory, publish_evidence_pack
from wtf_ingestion.market_data import collect_market, market_root
from wtf_ingestion.persistence.database import Base
from wtf_ingestion.persistence.models import CollectionRun
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher
from wtf_ingestion.registry import seed_registry

from catalyst_doot.app import create_app


@pytest.fixture
def accepted_app(tmp_path, fixture_root, monkeypatch):  # type: ignore[no-untyped-def]
    test_url = os.environ.get("CATALYST_TEST_DATABASE_URL")
    admin = create_engine(test_url) if test_url else None
    schema = "catalyst_test_" + uuid4().hex
    if admin is not None:
        assert admin.dialect.name == "postgresql", "PostgreSQL CI must not silently use SQLite"
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = admin.url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
            hide_password=False
        )
    else:
        url = f"sqlite+pysqlite:///{tmp_path / 'frontend.db'}"
    engine = create_engine(url)
    try:
        if admin is not None:
            monkeypatch.setenv("WTF_DATABASE_URL", url)
            get_settings.cache_clear()
            ingestion = Path(__file__).resolve().parents[2] / "ingestion"
            config = Config(str(ingestion / "alembic.ini"))
            config.set_main_option("script_location", str(ingestion / "migrations"))
            command.upgrade(config, "head")
        else:
            Base.metadata.create_all(engine)
        yield from _accepted_app(tmp_path, fixture_root, engine, url)
    finally:
        engine.dispose()
        get_settings.cache_clear()
        if admin is not None:
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def _accepted_app(tmp_path, fixture_root, engine, url):  # type: ignore[no-untyped-def]
    cfg = Settings(database_url=url, output_dir=tmp_path / "out", raw_dir=tmp_path / "raw")
    with Session(engine) as session:
        seed_registry(session)
        session.commit()
        raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
        candidate = SecEarningsAdapter(cfg).parse_fixture(raw, datetime.now(UTC))[0]
        service = IngestionService(session, cfg.raw_dir)
        service.ingest(
            source_key="sec_edgar",
            candidates=[candidate],
            raw=raw,
            source_url="https://data.sec.gov/test",
            content_type="application/json",
        )
        publication = Publisher(session, cfg.output_dir).publish()
        publish_evidence_pack(session, cfg)
        event_id = earnings_data(session, cfg)["data"][0]["event"]["event_id"]
    with TestClient(create_app(cfg)) as client:
        yield client, cfg, engine, candidate, publication, event_id


@pytest.mark.postgres
def test_migrated_postgres_views_match_publication_and_api(accepted_app):  # type: ignore[no-untyped-def]
    client, _, engine, candidate, publication, event_id = accepted_app
    if engine.dialect.name != "postgresql":
        pytest.skip("Set CATALYST_TEST_DATABASE_URL to run PostgreSQL view assertions")
    assert {"events_feed_current", "events_feed_horizon"} <= set(inspect(engine).get_view_names())
    response = client.get(f"/v1/earnings?publication_id={publication.publication_id}")
    assert response.status_code == 200
    assert response.json()["data"][0]["event"]["event_id"] == event_id
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "20261004_0010"
        # Both migrated views agree with the fixture's selected sample universe.
        horizon = conn.execute(text("SELECT * FROM events_feed_horizon")).mappings().one()
        assert str(horizon["event_id"]) == event_id
        assert horizon["event_date"] == candidate.event_date
        current = conn.execute(text("SELECT * FROM events_feed_current")).mappings().one()
        assert str(current["event_id"]) == event_id
    from wtf_ingestion.persistence.models import Company, UniverseMembership, UniverseVersion

    with Session(engine) as session:
        company = session.scalar(select(Company).where(Company.cik == candidate.cik))
        assert company is not None
        universe = UniverseVersion(
            name="Synthetic PostgreSQL scope",
            version="ci-v1",
            effective_from=datetime.now(UTC).date(),
            assumption=True,
            provenance={},
        )
        session.add(universe)
        session.flush()
        session.commit()
        universe_id, company_id = universe.id, company.id
    with engine.connect() as conn:
        # A new scope without this issuer withholds it from the consumer view.
        assert list(conn.execute(text("SELECT * FROM events_feed_current"))) == []
        assert conn.scalar(text("SELECT count(*) FROM events_feed_horizon")) == 1
    with Session(engine) as session:
        session.add(
            UniverseMembership(
                universe_version_id=universe_id,
                company_id=company_id,
                coverage_state="test",
                listings=[],
            )
        )
        session.commit()
    with engine.connect() as conn:
        current = conn.execute(text("SELECT * FROM events_feed_current")).mappings().one()
        assert str(current["event_id"]) == event_id
        assert current["revision_number"] == 1
        assert current["source_url"] == str(candidate.source_url)
        assert current["schema_version"] == "0.2.0"
    # A new active scope invalidates the old publication rather than relabelling it current.
    assert client.get("/v1/earnings").status_code == 503
    # Prove migration-installed append-only enforcement, not just view existence.
    for table in ("raw_objects", "event_versions", "source_assertions"):
        from sqlalchemy.exc import DBAPIError

        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as conn:
                conn.execute(Table(table, MetaData(), autoload_with=conn).delete())


def test_new_pages_and_removed_operator_ui(accepted_app):  # type: ignore[no-untyped-def]
    client, _, _, _, _, event_id = accepted_app
    for path in (
        "/",
        "/earnings",
        "/coverage",
        "/preferences",
        "/companies/AAPL",
        f"/events/{event_id}",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "Catalyst Doot" in response.text
        assert "/static/catalyst.js" in response.text
        assert 'data-evidence-mode="fixture"' in response.text
        assert "refresh-data" not in response.text
        assert "sage@" not in response.text
    assert client.get("/events?ticker=AAPL", follow_redirects=False).headers["location"] == (
        "/earnings?ticker=AAPL"
    )
    for path in ("/sources", "/runs", "/feeds", "/system", "/ocr-review", "/scan/preview"):
        assert client.get(path).status_code == 404
    assert client.get("/static/catalyst.css").status_code == 200


def test_market_read_contract_is_snapshot_bound_and_side_effect_free(accepted_app):  # type: ignore[no-untyped-def]
    client, cfg, _, _, publication, _ = accepted_app
    path = f"/v1/earnings/market?publication_id={publication.publication_id}"
    empty = client.get(path)
    assert empty.status_code == 200 and empty.json()["market_state"] == "UNAVAILABLE"
    assert empty.headers["cache-control"] == "no-store"
    assert not market_root(cfg).exists()
    assert client.get(f"/v1/earnings/market?publication_id={uuid4()}").status_code == 409
    assert client.get("/v1/earnings/market?publication_id=invalid").status_code == 422
    assert client.get(path + "&ticker=../escape").status_code == 422
    day = (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    requests = []

    def handler(request):  # type: ignore[no-untyped-def]
        requests.append(request)
        if request.url.path == "/v2/calendar":
            return httpx.Response(
                200, json=[{"date": day.date().isoformat(), "open": "09:30", "close": "16:00"}]
            )
        if request.url.path == "/v2/stocks/bars":
            return httpx.Response(
                200,
                json={
                    "bars": {
                        "AAPL": [
                            {"t": day.isoformat(), "o": 10, "h": 12, "l": 9, "c": 11, "v": 100}
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={"news": []} if request.url.path == "/v1beta1/news" else {"corporate_actions": {}},
        )

    collect_market(
        cfg,
        AlpacaClient(cfg, transport=httpx.MockTransport(handler)),
        calendar_publication_id=str(publication.publication_id),
        calendar_manifest_sha256=hashlib.sha256(
            (publication.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        scope=[
            {
                "ticker": "AAPL",
                "cik": "0000320193",
                "company_id": "test-issuer",
                "exchange": "NASDAQ",
            }
        ],
    )
    before = {
        str(p): (p.stat().st_mtime_ns, p.read_bytes())
        for parent in (market_root(cfg), cfg.raw_dir)
        for p in parent.rglob("*")
        if p.is_file()
    }
    loaded = client.get(path + "&ticker=AAPL")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["data"]["prices"][0]["close"] == "11"
    assert len(requests) == 4
    after = {
        str(p): (p.stat().st_mtime_ns, p.read_bytes())
        for parent in (market_root(cfg), cfg.raw_dir)
        for p in parent.rglob("*")
        if p.is_file()
    }
    assert before == after
    assert client.get("/static/market.js").status_code == 200
    pointer = market_root(cfg) / "current.json"
    pointer.write_text("{}")
    assert client.get(path).status_code == 503


def test_actual_history_read_is_snapshot_bound_and_never_builds(accepted_app):  # type: ignore[no-untyped-def]
    from wtf_ingestion.earnings_history import history_root

    client, cfg, _, _, publication, _ = accepted_app
    path = f"/v1/earnings/history?publication_id={publication.publication_id}"
    result = client.get(path)
    assert result.status_code == 200
    assert result.json()["history_state"] == "UNAVAILABLE"
    assert result.headers["cache-control"] == "no-store"
    assert not history_root(cfg).exists()
    assert client.get(f"/v1/earnings/history?publication_id={uuid4()}").status_code == 409
    assert client.get(path + "&ticker=../escape").status_code == 422
    assert client.get("/v1/earnings/history?publication_id=invalid").status_code == 422


def test_selected_evidence_is_exact_and_snapshot_races_fail(accepted_app):  # type: ignore[no-untyped-def]
    client, _, _, _, publication, event_id = accepted_app
    path = f"/v1/earnings/{event_id}/evidence"
    query = f"?publication_id={publication.publication_id}"
    data = client.get(path + query)
    assert data.status_code == 200, data.text
    assert data.json()["record"]["event"]["event_id"] == event_id
    assert data.json()["validation_scope"] == "selected_record_source_reproduction"
    assert client.get(path + f"?publication_id={uuid4()}").status_code == 409
    assert client.get(f"/v1/earnings/{uuid4()}/evidence" + query).status_code == 404
    assert client.get(path + "?publication_id=invalid").status_code == 422


def test_selected_evidence_rejects_changed_pack(accepted_app):  # type: ignore[no-untyped-def]
    client, cfg, _, _, publication, event_id = accepted_app
    path = pack_directory(cfg, str(publication.publication_id)) / "pack.json"
    body = json.loads(path.read_text())
    body["records"][0]["event"]["event_date"] = "2030-01-01"
    path.write_text(json.dumps(body))
    response = client.get(
        f"/v1/earnings/{event_id}/evidence?publication_id={publication.publication_id}"
    )
    assert response.status_code == 503
    assert "checksum" in response.json()["detail"]


def test_history_excludes_unpublished_revisions_and_keeps_retained_state(accepted_app):  # type: ignore[no-untyped-def]
    client, cfg, engine, candidate, publication, event_id = accepted_app
    with Session(engine) as session:
        service = IngestionService(session, cfg.raw_dir)
        revised = candidate.model_copy(
            update={
                "event_date": candidate.event_date + timedelta(days=1),
                "scheduled_start": candidate.scheduled_start + timedelta(days=1),
                "collected_at": datetime.now(UTC) + timedelta(seconds=1),
            }
        )
        service.ingest(
            source_key="sec_edgar",
            candidates=[revised],
            raw=b"new test revision",
            source_url="https://data.sec.gov/test",
            content_type="application/json",
        )
    response = client.get(
        f"/v1/earnings/{event_id}/history?publication_id={publication.publication_id}"
    )
    assert response.status_code == 200
    versions = response.json()["versions"]
    assert [item["revision"] for item in versions] == [1]
    assert versions[0]["facts"]["event_date"] == candidate.event_date.isoformat()
    with Session(engine) as session:
        run = session.scalar(select(CollectionRun))
        run.freshness_deadline = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert client.get("/v1/earnings").status_code == 503
    retained = client.get("/v1/earnings?retained=true").json()
    assert retained["is_current"] is False
    assert retained["data"][0]["event"]["event_date"] == candidate.event_date.isoformat()


def test_packaged_assets_headers_and_local_host_boundary(accepted_app):  # type: ignore[no-untyped-def]
    client, _, _, _, _, _ = accepted_app
    response = client.get("/")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for name in ("catalyst.css", "catalyst.js", "market.js", "history.js"):
        assert client.get("/static/" + name).status_code == 200
    assert client.get("/", headers={"host": "untrusted.example"}).status_code == 400


def test_dashboard_api_equals_collector_api(accepted_app):  # type: ignore[no-untyped-def]
    from wtf_ingestion.operations.web import create_app as create_collector_app

    client, cfg, _, _, _, _ = accepted_app
    with TestClient(create_collector_app(cfg)) as backend:
        dashboard = client.get("/v1/earnings").json()
        collector = backend.get("/v1/earnings").json()
        # Each read has its own request timestamp; published facts and trust match.
        assert datetime.fromisoformat(dashboard.pop("as_of")).tzinfo is not None
        assert datetime.fromisoformat(collector.pop("as_of")).tzinfo is not None
        assert dashboard == collector
        assert backend.get("/").status_code == 503
