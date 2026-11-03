from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
import uvicorn
from sqlalchemy import select

from wtf_ingestion.adapters.alpaca import AlpacaClient
from wtf_ingestion.adapters.base import SourceError
from wtf_ingestion.adapters.economic import IcsEconomicAdapter
from wtf_ingestion.adapters.ir import IssuerIrHtmlAdapter
from wtf_ingestion.adapters.sec import SecEarningsAdapter
from wtf_ingestion.adapters.sec_native import NATIVE_PARSER_VERSION
from wtf_ingestion.collectors import SecCollector
from wtf_ingestion.config import get_settings
from wtf_ingestion.earnings import earnings_data, read_snapshot
from wtf_ingestion.earnings_evidence import publish_evidence_pack
from wtf_ingestion.earnings_history import publish_history
from wtf_ingestion.logging import configure_logging
from wtf_ingestion.market_data import collect_market, scope_from_feed
from wtf_ingestion.operations.queries import overview
from wtf_ingestion.operations.web import create_app
from wtf_ingestion.persistence.database import session_scope
from wtf_ingestion.persistence.locking import pipeline_lock
from wtf_ingestion.persistence.models import CollectionRun, OperatorAction, Source, SourceEndpoint
from wtf_ingestion.pipeline import IngestionService
from wtf_ingestion.publication.publisher import Publisher
from wtf_ingestion.publication.trust import write_status
from wtf_ingestion.refresh import WORKER_LOCK, refresh_status, request_refresh, worker_tick
from wtf_ingestion.registry import seed_registry
from wtf_ingestion.universe import import_universe

app = typer.Typer(no_args_is_help=True, help="Operate the deterministic event ingestion service.")


def _configure() -> None:
    configure_logging(get_settings().log_level)


@app.command("seed-fixtures")
def seed_fixtures() -> None:
    """Load sanitized SEC, issuer IR, BLS, and BEA fixtures through production boundaries."""
    _configure()
    settings = get_settings()
    fixture_root = Path("tests/fixtures")
    observed = datetime.now(UTC)
    with session_scope() as session:
        if session.scalar(
            select(CollectionRun.id).where(CollectionRun.evidence_mode == "live").limit(1)
        ):
            raise RuntimeError("fixture seeding is refused in a database containing live evidence")
        seed_registry(session)
        session.commit()
        service = IngestionService(
            session, settings.raw_dir, settings.freshness_hours, output_dir=settings.output_dir
        )
        sec_raw = (fixture_root / "sec/explicit_announcement.json").read_bytes()
        sec = SecEarningsAdapter(settings)
        _emit_result(
            service.ingest(
                source_key="sec_edgar",
                candidates=sec.parse_fixture(sec_raw, observed),
                raw=sec_raw,
                source_url="https://www.sec.gov/Archives/edgar/data/fixture",
                content_type="application/json",
            )
        )
        ir_raw = (fixture_root / "ir/official_announcement.html").read_bytes()
        ir = IssuerIrHtmlAdapter(
            source_id="issuer_demo",
            ticker="AMZN",
            exchange="NASDAQ",
            company_name="Amazon.com, Inc.",
            cik="1018724",
            source_url="https://investor.example.invalid/releases/fixture-q3-2026",
        )
        _emit_result(
            service.ingest(
                source_key="issuer_demo",
                candidates=ir.parse_fixture(ir_raw, observed),
                raw=ir_raw,
                source_url="https://investor.example.invalid/releases/fixture-q3-2026",
                content_type="text/html",
            )
        )
        for source_key, filename, owner, url in (
            (
                "bls_calendar",
                "bls.ics",
                "U.S. Bureau of Labor Statistics",
                "https://www.bls.gov/schedule/news_release/bls.ics",
            ),
            (
                "bea_calendar",
                "bea.ics",
                "U.S. Bureau of Economic Analysis",
                "https://www.bea.gov/news/schedule/icalendar",
            ),
        ):
            raw = (fixture_root / "economic" / filename).read_bytes()
            adapter = IcsEconomicAdapter(source_id=source_key, owner=owner, source_url=url)
            _emit_result(
                service.ingest(
                    source_key=source_key,
                    candidates=adapter.parse(raw, observed),
                    raw=raw,
                    source_url=url,
                    content_type="text/calendar",
                )
            )


def _emit_result(result) -> None:  # type: ignore[no-untyped-def]
    typer.echo(json.dumps({**result.__dict__, "run_id": str(result.run_id)}, sort_keys=True))


@app.command()
def publish(evidence_mode: str = typer.Option("fixture", help="fixture or live")) -> None:
    """Create validated versioned JSONL/CSV and atomically move the current pointer."""
    _configure()
    settings = get_settings()
    with session_scope() as session:
        result = Publisher(session, settings.output_dir, settings.publication_horizon_days).publish(
            evidence_mode=evidence_mode
        )
        typer.echo(
            json.dumps(
                {
                    **result.__dict__,
                    "publication_id": str(result.publication_id),
                    "directory": str(result.directory),
                },
                sort_keys=True,
            )
        )


@app.command("build-earnings-evidence")
def build_earnings_evidence() -> None:
    """Build a source-verified pack for the accepted publication; no source requests."""
    _configure()
    with session_scope() as session:
        typer.echo(json.dumps(publish_evidence_pack(session, get_settings()), sort_keys=True))


@app.command("collect-alpaca")
def collect_alpaca(build_history: bool = typer.Option(True)) -> None:
    """Publish approved market data, then actual retained earnings history and reactions."""
    cfg = get_settings()
    try:
        cfg.assert_live_alpaca_ready()
        with session_scope() as session, pipeline_lock(session):
            feed = earnings_data(session, cfg)
            publication, _ = read_snapshot(session, cfg)
            result = collect_market(
                cfg,
                AlpacaClient(cfg),
                calendar_publication_id=str(publication.id),
                calendar_manifest_sha256=publication.manifest_sha256,
                scope=scope_from_feed(feed),
            )
            if build_history:
                result["earnings_history"] = publish_history(session, cfg)
        typer.echo(json.dumps(result, sort_keys=True))
    except (RuntimeError, SourceError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


@app.command("build-earnings-history")
def build_earnings_history() -> None:
    """Publish actual retained releases and deterministic reactions; no source requests."""
    with session_scope() as session, pipeline_lock(session):
        typer.echo(json.dumps(publish_history(session, get_settings()), sort_keys=True))


@app.command()
def status(write_sidecar: bool = typer.Option(False)) -> None:
    """Read redacted state; optionally refresh the sidecar from a writable runtime."""
    _configure()
    with session_scope() as session:
        data = overview(session)
        payload = {
            "publication": {
                "id": str(data["publication"].id),
                "state": data["feed_state"],
                "generated_at": data["publication"].generated_at.isoformat(),
                "records": data["publication"].record_count,
                "evidence_mode": data["publication"].evidence_mode,
            }
            if data["publication"]
            else None,
            "coverage": {
                "covered": data["covered"],
                "announced": data["covered"],
                "estimated": data["estimated_count"],
                "total": data["universe_total"],
                "percent": data["coverage_percent"],
            },
            "sources": [
                {
                    "key": row["source"].key,
                    "state": row["state"],
                    "live_enabled": row["source"].live_enabled,
                    "fixture_only": row["source"].fixture_only,
                    "last_run_id": str(row["run"].id) if row["run"] else None,
                }
                for row in data["sources"]
            ],
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        if write_sidecar:
            write_status(session, get_settings().output_dir)


@app.command("import-universe")
def universe_command(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Import a validated immutable fixed-universe JSON version."""
    with session_scope() as session:
        typer.echo(import_universe(session, path))


@app.command("collect-sec")
def collect_sec() -> None:
    """Run the approved fixed-universe SEC scan; no fixture-to-live relabelling."""
    _configure()
    with session_scope() as session:
        _emit_result(SecCollector(get_settings(), session).run())


@app.command("request-refresh")
def queue_refresh(idempotency_key: str = typer.Option(...)) -> None:
    """Queue one audited refresh for the local worker; never bypass source gates."""
    if not idempotency_key.strip() or len(idempotency_key) > 100:
        raise typer.BadParameter("provide a short unique idempotency key")
    with session_scope() as session:
        job = request_refresh(
            session, get_settings(), idempotency_key=idempotency_key, actor="local-cli"
        )
        typer.echo(json.dumps({"job_id": str(job.id), "state": job.state}))


@app.command("recover-publication")
def recover_publication(publication_id: UUID) -> None:
    """Re-validate and activate a committed publication after an interrupted pointer write."""
    with session_scope() as session:
        Publisher(session, get_settings().output_dir).recover_pointer(publication_id)
        typer.echo("Validated publication pointer recovered.")


@app.command("enable-sec")
def enable_sec() -> None:
    """Enable SEC after approved runtime identity is present; does not issue a request."""
    get_settings().assert_live_sec_ready()
    with session_scope() as session:
        seed_registry(session)
        session.flush()
        source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
        if source is None:
            seed_registry(session)
            session.flush()
            source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
        if source is None:
            raise RuntimeError("SEC registry entry is required")
        if source.permission_status == "access_review_required":
            raise RuntimeError(
                "SEC access incident requires fresh operator/source review; "
                "the existing identity approval cannot re-enable a blocked source"
            )
        source.permission_status = "approved"
        source.fixture_only = False
        source.live_enabled = True
        source.parser_version = NATIVE_PARSER_VERSION
        source.reviewed_at = date(2026, 10, 1)
        for endpoint in session.scalars(
            select(SourceEndpoint).where(SourceEndpoint.source_id == source.id)
        ):
            endpoint.approval_state = "approved"
        identity = f"enable-sec-{source.id}"
        if not session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == identity)
        ):
            session.add(
                OperatorAction(
                    action_type="enable_source",
                    actor="Abdul (Sage approval)",
                    target_id="sec_edgar",
                    reason="Sage approved SEC identity on 2026-10-01; access terms reviewed.",
                    idempotency_key=identity,
                )
            )
        session.commit()
        typer.echo("SEC source enabled locally; no network request was made by this command.")


@app.command("retry-sec")
def retry_sec(reason: str = typer.Option(...), idempotency_key: str = typer.Option(...)) -> None:
    """Audit one operator-requested SEC retry before starting its gated collection."""
    _configure()
    get_settings().assert_live_sec_ready()
    if not reason.strip() or len(reason) > 500 or len(idempotency_key) > 100:
        raise typer.BadParameter("provide a short reason and unique idempotency key")
    with session_scope() as session:
        existing = session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == idempotency_key)
        )
        if existing:
            typer.echo("Retry already requested; no duplicate collection dispatched.")
            return
        session.add(
            OperatorAction(
                action_type="retry",
                actor="local-cli",
                target_id="sec_edgar",
                reason=reason,
                idempotency_key=idempotency_key,
            )
        )
        session.commit()
        _emit_result(SecCollector(get_settings(), session).run(trigger="audited_retry"))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8765, min=1, max=65535)
) -> None:
    """Serve local operations with an audited refresh-request control."""
    _configure()
    uvicorn.run(create_app(), host=host, port=port)


@app.command("scheduled-refresh")
def scheduled_refresh(
    interval_hours: int = typer.Option(24, min=1, max=168),
    once: bool = typer.Option(False),
) -> None:
    """Run the gated scheduler loop; refuse to imply collection when no live source is enabled."""
    _configure()
    settings = get_settings()
    with session_scope() as session:
        if not session.scalar(select(Source.id).where(Source.live_enabled.is_(True))):
            typer.echo("No live source is enabled; approved prerequisites are required.", err=True)
            raise typer.Exit(code=2)
    with session_scope() as lease_session, pipeline_lock(lease_session, WORKER_LOCK):
        while True:
            with session_scope() as session:
                worker_tick(session, settings, interval_hours, force=once)
                write_status(session, settings.output_dir)
                if once:
                    typer.echo(json.dumps(refresh_status(session)))
                    return
            time.sleep(5)


if __name__ == "__main__":
    app()
