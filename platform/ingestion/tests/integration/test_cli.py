from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from wtf_ingestion.cli import app
from wtf_ingestion.config import Settings


def test_cli_gates_live_sec_and_scheduler(db_session, tmp_path):  # type: ignore[no-untyped-def]
    from contextlib import contextmanager

    @contextmanager
    def fake_session():  # type: ignore[no-untyped-def]
        yield db_session

    settings = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    with (
        patch("wtf_ingestion.cli.session_scope", fake_session),
        patch("wtf_ingestion.cli.get_settings", return_value=settings),
    ):
        runner = CliRunner()
        assert runner.invoke(app, ["--help"]).exit_code == 0
        assert runner.invoke(app, ["scheduled-refresh", "--once"]).exit_code == 2
        assert runner.invoke(app, ["collect-sec"]).exit_code != 0
        assert (
            runner.invoke(
                app, ["retry-sec", "--reason", "test", "--idempotency-key", "test-1"]
            ).exit_code
            != 0
        )
        assert runner.invoke(app, ["seed-fixtures"]).exit_code == 0
        assert runner.invoke(app, ["publish"]).exit_code == 0
        with patch("wtf_ingestion.cli.write_status") as writer:
            result = runner.invoke(app, ["status"])
            writer.assert_not_called()
        assert result.exit_code == 0 and '"evidence_mode": "fixture"' in result.stdout
        with patch("wtf_ingestion.cli.write_status") as writer:
            assert runner.invoke(app, ["status", "--write-sidecar"]).exit_code == 0
            writer.assert_called_once()


def test_enable_sec_cannot_bypass_access_review(db_session):  # type: ignore[no-untyped-def]
    from contextlib import contextmanager

    from sqlalchemy import select

    from wtf_ingestion.persistence.models import Source

    @contextmanager
    def fake_session():  # type: ignore[no-untyped-def]
        yield db_session

    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    assert source
    source.live_enabled = False
    source.permission_status = "access_review_required"
    db_session.commit()
    settings = Settings(
        enable_live_sec=True, sec_user_agent="Fixture Organization fixture@example.invalid"
    )
    with (
        patch("wtf_ingestion.cli.session_scope", fake_session),
        patch("wtf_ingestion.cli.get_settings", return_value=settings),
    ):
        result = CliRunner().invoke(app, ["enable-sec"])
    assert result.exit_code != 0
    assert source.permission_status == "access_review_required" and not source.live_enabled


def test_scheduler_waits_until_due_instead_of_double_fetch(db_session, tmp_path):  # type: ignore[no-untyped-def]
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from wtf_ingestion.persistence.models import CollectionRun, Source

    @contextmanager
    def fake_session():  # type: ignore[no-untyped-def]
        yield db_session

    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.live_enabled, source.fixture_only, source.permission_status = True, False, "approved"
    now = datetime.now(UTC)
    db_session.add(
        CollectionRun(
            source_id=source.id,
            trigger="manual",
            evidence_mode="live",
            state="HEALTHY",
            stage="persist",
            started_at=now,
            freshness_deadline=now + timedelta(hours=36),
        )
    )
    db_session.commit()
    cfg = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    with (
        patch("wtf_ingestion.cli.session_scope", fake_session),
        patch("wtf_ingestion.cli.get_settings", return_value=cfg),
        patch("wtf_ingestion.refresh.SecCollector") as collector,
        patch("wtf_ingestion.cli.time.sleep", side_effect=RuntimeError("test stop")) as sleep,
    ):
        result = CliRunner().invoke(app, ["scheduled-refresh"])
    assert result.exit_code != 0 and str(result.exception) == "test stop"
    collector.assert_not_called()
    # Poll the local job queue every five seconds; SEC collection still waits 24 hours.
    assert sleep.call_args.args[0] == 5
