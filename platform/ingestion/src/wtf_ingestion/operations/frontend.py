"""Local earnings research interface over immutable accepted publications."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from wtf_ingestion.adapters.base import SourceError
from wtf_ingestion.config import Settings, get_settings
from wtf_ingestion.earnings import EarningsUnavailable, earnings_data, read_snapshot
from wtf_ingestion.earnings_evidence import (
    EvidenceUnavailable,
    _read_pack_files,
    claims_for,
    document_evidence,
    encoded,
    identity_basis,
    read_evidence_pack,
)
from wtf_ingestion.earnings_history import HistoryUnavailable, read_history
from wtf_ingestion.health import as_utc
from wtf_ingestion.market_data import MarketUnavailable, read_market
from wtf_ingestion.persistence.database import make_engine
from wtf_ingestion.persistence.models import (
    CollectionRun,
    EventVersion,
    PublicationManifest,
    Source,
    UniverseVersion,
)
from wtf_ingestion.publication.trust import publication_trust
from wtf_ingestion.refresh import RefreshRejected, refresh_status, request_refresh
from wtf_ingestion.universe import active_universe

PACKAGE_DIR = Path(__file__).parent


def selected_record(
    session: Session, cfg: Settings, event_id: UUID, publication_id: UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    research = earnings_data(session, cfg, retained=True)
    if research["publication_id"] != str(publication_id):
        raise HTTPException(409, "The accepted snapshot changed. Reload the page to continue.")
    row = next((row for row in research["data"] if row["event"]["event_id"] == str(event_id)), None)
    if row is None:
        raise HTTPException(404, "Event is not in this accepted publication.")
    return research, row


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    engine = make_engine(cfg.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI(title="Catalyst Doot — Earnings", version="0.1.0")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    templates = (
        Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
        if (PACKAGE_DIR / "templates").is_dir()
        else None
    )
    if (PACKAGE_DIR / "static").is_dir():
        app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    signing_key = secrets.token_bytes(32)

    def valid_token(token: str) -> bool:
        parts = token.split(".")
        return (
            len(parts) == 2
            and len(parts[0]) == 64
            and hmac.compare_digest(
                parts[1], hmac.new(signing_key, parts[0].encode(), hashlib.sha256).hexdigest()
            )
        )

    def get_session():  # type: ignore[no-untyped-def]
        with factory() as session:
            if cfg.dashboard_mode == "live" and session.scalar(
                select(CollectionRun.id).where(CollectionRun.evidence_mode == "fixture").limit(1)
            ):
                raise HTTPException(503, "Live earnings refuse fixture evidence.")
            yield session

    @app.get("/healthz")
    def health(session: Session = Depends(get_session)) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    @app.get("/earnings", include_in_schema=False)
    @app.get("/coverage", include_in_schema=False)
    @app.get("/preferences", include_in_schema=False)
    @app.get("/companies/{ticker}", include_in_schema=False)
    @app.get("/events/{event_id}", include_in_schema=False)
    def page(request: Request) -> Response:
        if templates is None:
            raise HTTPException(
                503, "Catalyst dashboard assets are not installed in this collector."
            )
        return templates.TemplateResponse(
            request,
            "catalyst.html",
            {"evidence_mode": cfg.dashboard_mode},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/events", include_in_schema=False)
    def old_events(request: Request) -> RedirectResponse:
        query = request.url.query
        return RedirectResponse("/earnings" + ("?" + query if query else ""), status_code=307)

    @app.get("/v1/earnings/market")
    def market_api(
        publication_id: UUID,
        ticker: str | None = Query(None, max_length=15, pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$"),
        retained: bool = False,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        try:
            publication, _ = read_snapshot(session, cfg)
            if str(publication.id) != str(publication_id):
                raise HTTPException(
                    409, "The accepted calendar snapshot changed. Reload to continue."
                )
            data = read_market(
                cfg,
                calendar_publication_id=str(publication.id),
                calendar_manifest_sha256=publication.manifest_sha256,
                ticker=ticker,
                retained=retained,
            )
            data["calendar_state"] = publication_trust(session, publication)
        except (MarketUnavailable, EarningsUnavailable) as exc:
            raise HTTPException(503, str(exc)) from None
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/v1/earnings/history")
    def history_api(
        publication_id: UUID,
        ticker: str | None = Query(None, max_length=15, pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$"),
        retained: bool = False,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        try:
            publication, _ = read_snapshot(session, cfg)
            if str(publication.id) != str(publication_id):
                raise HTTPException(
                    409, "The accepted calendar snapshot changed. Reload to continue."
                )
            data = read_history(
                cfg,
                calendar_publication_id=str(publication.id),
                calendar_manifest_sha256=publication.manifest_sha256,
                ticker=ticker,
                retained=retained,
            )
            data["calendar_state"] = publication_trust(session, publication)
            if data["calendar_state"] != "HEALTHY":
                data["is_current"] = False
                if not retained:
                    data["data"] = None
                    data["history_state"] = data["calendar_state"]
                    data["reason"] = "Current calendar trust is unavailable."
        except (HistoryUnavailable, EarningsUnavailable, MarketUnavailable) as exc:
            raise HTTPException(503, str(exc)) from None
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/v1/earnings")
    def earnings_api(
        ticker: str | None = None,
        retained: bool = False,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        try:
            data = earnings_data(session, cfg, retained=retained)
        except EarningsUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        if ticker:
            name = ticker.strip().upper()
            data["data"] = [
                row
                for row in data["data"]
                if row["event"]["ticker"] == name
                or any(listing["ticker"] == name for listing in row["listings"])
            ]
            data["missing_issuers"] = [
                row
                for row in data["missing_issuers"]
                if any(listing["ticker"] == name for listing in row["listings"])
            ]
        total = len(data["data"])
        data["data"] = data["data"][offset : offset + limit]
        data["pagination"] = {"offset": offset, "limit": limit, "total": total}
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/v1/earnings/evidence")
    def earnings_evidence_api(
        ticker: str | None = None,
        retained: bool = False,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        try:
            data = read_evidence_pack(session, cfg, retained=retained)
        except (EvidenceUnavailable, EarningsUnavailable) as exc:
            raise HTTPException(503, str(exc)) from None
        if ticker:
            name = ticker.strip().upper()
            data["records"] = [
                row
                for row in data["records"]
                if row["event"]["ticker"] == name
                or any(listing["ticker"] == name for listing in row["listings"])
            ]
            data["missing_issuers"] = [
                row
                for row in data["missing_issuers"]
                if any(listing["ticker"] == name for listing in row["listings"])
            ]
        total = len(data["records"])
        data["records"] = data["records"][offset : offset + limit]
        data["pagination"] = {"offset": offset, "limit": limit, "total": total}
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/v1/earnings/{event_id}/evidence")
    def event_evidence(
        event_id: UUID,
        publication_id: UUID,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        """Verify only the selected record's source reproduction; never fetch or build."""
        try:
            research, row = selected_record(session, cfg, event_id, publication_id)
            publication, _ = read_snapshot(session, cfg)
            if str(publication.id) != research["publication_id"]:
                raise EvidenceUnavailable("Publication changed during evidence validation.")
            payload, _, _ = _read_pack_files(cfg, publication)
            universe = session.get(UniverseVersion, publication.universe_version_id)
            expected_ids = {item["event"]["event_id"] for item in research["data"]}
            items = payload["records"]
            if (
                universe is None
                or payload["identity_basis"] != identity_basis(universe)
                or payload["publication_generated_at"] != research["publication_generated_at"]
                or payload["briefs_generated"] is not False
                or payload["prices_included"] is not False
                or len(items) != len(expected_ids)
                or {item["event"]["event_id"] for item in items} != expected_ids
                or payload["missing_issuers"]
                != sorted(research["missing_issuers"], key=lambda item: item["cik"])
            ):
                raise EvidenceUnavailable("Evidence scope does not match the accepted snapshot.")
            item = next(item for item in items if item["event"]["event_id"] == str(event_id))
            material = {key: value for key, value in item.items() if key != "evidence_pack_id"}
            if (
                hashlib.sha256(encoded(material)).hexdigest() != item["evidence_pack_id"]
                or item["event"] != row["event"]
                or item["company_id"] != row["company_id"]
                or item["listings"] != row["listings"]
                or item["fiscal"] != row["fiscal"]
                or item["claims"] != claims_for(row)
                or item["evidence"]
                != [document_evidence(row, cfg, {}, fixture=publication.evidence_mode == "fixture")]
                or item["unavailable_fields"]
                != {
                    name: field["reason"]
                    for name, field in row["fields"].items()
                    if field["value"] is None
                }
            ):
                raise EvidenceUnavailable("Selected evidence does not reproduce accepted facts.")
            return JSONResponse(
                {
                    "record": item,
                    "publication_id": research["publication_id"],
                    "calendar_state": research["calendar_state"],
                    "is_current": research["is_current"],
                    "validation_scope": "selected_record_source_reproduction",
                },
                headers={"Cache-Control": "no-store"},
            )
        except (EvidenceUnavailable, EarningsUnavailable) as exc:
            raise HTTPException(503, str(exc)) from None
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            StopIteration,
            SourceError,
        ) as exc:
            raise HTTPException(503, "Selected evidence is unavailable or malformed.") from exc

    @app.get("/v1/earnings/{event_id}/history")
    def event_history(
        event_id: UUID,
        publication_id: UUID,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        try:
            research, row = selected_record(session, cfg, event_id, publication_id)
        except EarningsUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        versions = session.scalars(
            select(EventVersion)
            .where(
                EventVersion.event_id == event_id,
                EventVersion.revision_number <= row["event_revision"],
            )
            .order_by(EventVersion.revision_number.desc())
        ).all()
        fields = (
            "event_date",
            "scheduled_start",
            "session",
            "fiscal_period",
            "status",
            "source_url",
            "date_basis",
            "time_precision",
        )
        return JSONResponse(
            {
                "publication_id": research["publication_id"],
                "versions": [
                    {
                        "revision": item.revision_number,
                        "recorded_at": as_utc(item.valid_from).isoformat(),
                        "facts": {key: item.payload.get(key) for key in fields},
                    }
                    for item in versions
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    # Existing operator APIs stay available to tooling; the consumer has no collection controls.
    @app.get("/refresh/status")
    def refresh_state(session: Session = Depends(get_session)) -> JSONResponse:
        nonce = secrets.token_hex(32)
        token = nonce + "." + hmac.new(signing_key, nonce.encode(), hashlib.sha256).hexdigest()
        data = refresh_status(session)
        publication = session.scalar(
            select(PublicationManifest)
            .where(PublicationManifest.activated_at.is_not(None))
            .order_by(PublicationManifest.generated_at.desc())
            .limit(1)
        )
        from wtf_ingestion.publication.trust import publication_trust

        data["feed_state"] = publication_trust(session, publication)
        data["csrf_token"] = token
        source = session.scalar(select(Source).where(Source.key == "sec_edgar"))
        if cfg.dashboard_mode != "live":
            reason = "Source refresh is available only in the live dashboard."
        elif not cfg.enable_live_sec or not cfg.sec_user_agent:
            reason = "The live SEC collector or approved request identity is not configured."
        elif (
            not source
            or not source.live_enabled
            or source.fixture_only
            or source.permission_status != "approved"
        ):
            reason = "SEC access is disabled or requires review. Refresh cannot override a block."
        elif active_universe(session) is None:
            reason = "A reviewed fixed company list is required before refreshing."
        else:
            reason = ""
        data["enabled"] = not reason
        if not reason and not data["worker_online"]:
            reason = "The local refresh worker is offline. Start or recover the Docker scheduler."
        elif not reason and data["job"] and data["job"]["state"] in {"queued", "running"}:
            reason = "A refresh is already queued or running."
        elif not reason and data["cooldown_seconds"] > 0:
            reason = "Manual refresh has a 15-minute cooldown after the previous request."
        data["disabled_reason"] = reason
        response = JSONResponse(data, headers={"Cache-Control": "no-store"})
        response.set_cookie("refresh_csrf", token, httponly=True, samesite="strict")
        return response

    @app.post("/refresh")
    def refresh_request(request: Request, session: Session = Depends(get_session)) -> JSONResponse:
        if cfg.dashboard_mode != "live":
            raise HTTPException(403, "Manual source refresh is available only in live mode.")
        token = request.headers.get("x-csrf-token", "")
        if (
            request.headers.get("origin", "") != str(request.base_url).rstrip("/")
            or not valid_token(token)
            or not hmac.compare_digest(token, request.cookies.get("refresh_csrf", ""))
        ):
            raise HTTPException(403, "Same-origin refresh approval token required.")
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 100:
            raise HTTPException(400, "A short idempotency key is required.")
        try:
            job = request_refresh(session, cfg, idempotency_key=key)
        except (RefreshRejected, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse({"job_id": str(job.id), "state": job.state}, status_code=202)

    @app.get("/feeds/{publication_id}/{filename}")
    def download_feed(
        publication_id: UUID, filename: str, session: Session = Depends(get_session)
    ) -> FileResponse:
        if filename not in {"events.jsonl", "events.csv", "manifest.json"}:
            raise HTTPException(404, "File not found")
        publication = session.get(PublicationManifest, publication_id)
        if publication is None:
            raise HTTPException(404, "Publication not found")
        name = publication.output_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        root = (cfg.output_dir / name).resolve()
        path = (root / filename).resolve()
        if root.parent != cfg.output_dir.resolve() or path.parent != root or not path.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(path, filename=filename)

    @app.on_event("shutdown")
    def dispose_engine() -> None:
        engine.dispose()

    return app
