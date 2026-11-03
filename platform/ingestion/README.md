# Earnings ingestion

**Responsibility:** multi-source public-data collectors, dedup, source-tagged feed
**Scope:** earnings first. SEC supplies live earnings evidence; official issuer IR still
requires per-host onboarding. IR and macro adapters are fixture-only, and macro and
FDA/PDUFA production collectors are later scope. Alpaca is a historical market-data
companion, not an earnings-date source. Brief/model work remains deferred.

**Workflow:** feature branch -> PR into main. Review before merging.

## Local fixture review

Requires Python 3.12, uv and Docker. From the repository root:

```sh
cd platform/ingestion
uv sync --locked --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run wtf-ingestion seed-fixtures
uv run wtf-ingestion publish
uv run wtf-ingestion build-earnings-evidence
uv run wtf-ingestion status
uv run wtf-ingestion serve --port 8766
```

Defaults use synthetic fixtures, disabled live sources, loopback PostgreSQL port 55433
and a separate `dev0sim-earnings-collector` Compose volume. Keep this namespace separate
from retained live data; never reset a data volume to review the package. PostgreSQL
`trust` authentication is local development only; deployment needs authenticated settings.

Read the JSON API at `http://127.0.0.1:8766/v1/earnings`. The HTML dashboard belongs to
the sibling `platform/catalyst-doot` package; ingestion alone
returns 503 for the HTML page. Serving either package does not start collection.

## Verification and operation

```sh
uv run ruff check src tests migrations scripts tools
uv run ruff format --check src tests migrations scripts tools
uv run mypy src
uv run pytest
uv build
```

For PostgreSQL integration tests, migrate a disposable database and set
`WTF_TEST_DATABASE_URL` to its SQLAlchemy PostgreSQL URL. Migration regression tests
use `WTF_MIGRATION_TEST_DATABASE_URL` and create only temporary schemas in that test
database. Tests use synthetic evidence and mocked HTTP; no live collection is needed.

**Live Alpaca collection is stopped pending Sage's feed confirmation.** Do not run
`collect-alpaca` while this hold applies. Live SEC needs the approved identity supplied
through `WTF_SEC_USER_AGENT`, reviewed runtime scope and explicit enablement. Keep
credentials/runtime identities out of code. A 403/429 stops access; ordinary refresh
cannot clear the persisted block.

Publications retain raw evidence and immutable assertions/revisions. Consumers must
verify publication IDs, hashes, source/run health and freshness deadlines; a retained
snapshot or database view alone does not establish current data. Unknown dates/clocks
remain unknown, and estimates remain labelled. OCR models are already pinned to an
official commit with SHA-256 checks in `tools/ocr/download_models.py`.

## The four rules (non-negotiable)
1. Private repo only — no forks, mirrors, or public pushes.
2. Credentials over WhatsApp only — keys/tokens never in email, Slack, or the code. Use env vars.
3. Ask before you spend — paid APIs, cloud/GPU, hardware -> check with the platform lead first.
4. Synthetic / public data only — no real personal or location data (no PII).
