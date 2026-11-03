"""FastAPI entrypoint for the local Catalyst Doot earnings frontend."""

from wtf_ingestion.operations.frontend import create_app as create_app

app = create_app()
