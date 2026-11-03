from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from wtf_ingestion.adapters.alpaca import AlpacaClient
from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.config import Settings
from wtf_ingestion.earnings import earnings_data
from wtf_ingestion.earnings_evidence import pack_directory, publish_evidence_pack
from wtf_ingestion.market_data import collect_market, market_root
from wtf_ingestion.operations.web import create_app
from wtf_ingestion.persistence.database import Base
from wtf_ingestion.persistence.models import CollectionRun
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher
from wtf_ingestion.registry import seed_registry


@pytest.fixture
def accepted_app(tmp_path, fixture_root):  # type: ignore[no-untyped-def]
    url = f"sqlite+pysqlite:///{tmp_path / 'frontend.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
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
    engine.dispose()


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


def test_collector_api_without_catalyst_assets(accepted_app):  # type: ignore[no-untyped-def]
    client, _, _, _, _, _ = accepted_app
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/v1/earnings").status_code == 200
    assert client.get("/").status_code == 503
    assert client.get("/static/catalyst.js").status_code == 404
    assert client.get("/healthz", headers={"host": "untrusted.example.invalid"}).status_code == 400
