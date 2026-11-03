"""Serve the existing earnings interface with the shared ingestion read API."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from wtf_ingestion.config import Settings, get_settings
from wtf_ingestion.operations.web import create_app as create_ingestion_app

PACKAGE_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(title="Catalyst Doot — Earnings", version="0.1.0")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/earnings", include_in_schema=False)
    @app.get("/coverage", include_in_schema=False)
    @app.get("/preferences", include_in_schema=False)
    @app.get("/companies/{ticker}", include_in_schema=False)
    @app.get("/events/{event_id}", include_in_schema=False)
    def page(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "catalyst.html",
            {"evidence_mode": cfg.dashboard_mode},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    # Own HTML routes precede the collector's missing-assets fallbacks. Reuse its
    # API handlers and publication checks instead of copying numerical/fact logic.
    app.include_router(create_ingestion_app(cfg).router)
    return app
