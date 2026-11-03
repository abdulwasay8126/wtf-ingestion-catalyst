from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from wtf_ingestion import ocr_trial
from wtf_ingestion.adapters.base import (
    AccessBlockedError,
    FetchPolicy,
    PayloadError,
    SafeHttpClient,
    SourcePermissionError,
    ThrottledError,
)
from wtf_ingestion.config import Settings
from wtf_ingestion.persistence.models import EventCurrent, OperatorAction, Source


@pytest.mark.parametrize("fault", [None, "blocked", "throttled", "content_type", "unsafe"])
def test_ocr_capture_is_audited_and_never_publishes(db_session, tmp_path, monkeypatch, fault):  # type: ignore[no-untyped-def]
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.permission_status = "approved"
    source.live_enabled = True
    source.fixture_only = False
    db_session.commit()
    monkeypatch.setattr(
        ocr_trial,
        "TRIAL_DOCUMENTS",
        (("AAPL", "0000320193", "0000320193-26-000099", "release.htm"),),
    )
    visited = []

    def respond(request):  # type: ignore[no-untyped-def]
        visited.append(str(request.url))
        if request.url.path.endswith("-index.html"):
            body = (
                b'<table summary="Document Format Files"><tr><td>2</td><td>Page</td>'
                b'<td><a href="page.jpg">page.jpg</a></td><td>GRAPHIC</td></tr></table>'
            )
            mime = "text/html"
        elif request.url.path.endswith(".htm"):
            body = (
                b'<img src="https://evil.invalid/page.jpg">'
                if fault == "unsafe"
                else b'<img src="page.jpg">'
            )
            mime = "text/html"
        else:
            if fault in {"blocked", "throttled"}:
                return httpx.Response(403 if fault == "blocked" else 429)
            body = b"Sanitized image capture fixture; recognition is a separate boundary."
            mime = "text/plain" if fault == "content_type" else "image/jpeg"
        return httpx.Response(200, headers={"content-type": mime}, content=body)

    def offline(policy: FetchPolicy) -> SafeHttpClient:
        assert policy.stop_on_throttle and policy.min_interval_seconds == 0.5
        return SafeHttpClient(
            replace(policy, min_interval_seconds=0), transport=httpx.MockTransport(respond)
        )

    monkeypatch.setattr(ocr_trial, "SafeHttpClient", offline)
    cfg = Settings(
        output_dir=tmp_path / "out/publications",
        enable_live_sec=True,
        sec_user_agent="Offline Test qa@example.invalid",
    )
    if fault:
        expected = {
            "blocked": AccessBlockedError,
            "throttled": ThrottledError,
            "unsafe": SourcePermissionError,
            "content_type": PayloadError,
        }[fault]
        with pytest.raises(expected):
            ocr_trial.capture_trial(db_session, cfg)
    else:
        result = ocr_trial.capture_trial(db_session, cfg)
        manifest = json.loads(result.read_text())
        assert manifest["state"] == "CAPTURED_REVIEW_ONLY"
        assert len(manifest["http_attempts"]) == 3
        page = manifest["documents"][0]["pages"][0]
        assert (
            hashlib.sha256((result.parent / page["file"]).read_bytes()).hexdigest()
            == page["sha256"]
        )
        assert page["retrieved_at"]
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 0
    if fault in {"blocked", "throttled"}:
        assert not source.live_enabled
        assert db_session.scalar(select(OperatorAction)).action_type == "ocr_trial_access_incident"
    if fault == "unsafe":
        assert len(visited) == 2


def test_ocr_trial_refuses_unapproved_source_and_oversized_sample(db_session, tmp_path):  # type: ignore[no-untyped-def]
    cfg = Settings(
        output_dir=tmp_path / "out/publications",
        enable_live_sec=True,
        sec_user_agent="Offline Test qa@example.invalid",
    )
    with pytest.raises(SourcePermissionError):
        ocr_trial.capture_trial(db_session, cfg)
    with pytest.raises(ValueError, match="three sample"):
        ocr_trial.capture_trial(db_session, cfg, sample_limit=150)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("reuse_fault", [None, "tampered", "stale", "future", "changed_index"])
def test_full_capture_rechecks_membership_reuses_only_valid_images(
    db_session, tmp_path, monkeypatch, reuse_fault
):  # type: ignore[no-untyped-def]
    source = db_session.scalar(select(Source).where(Source.key == "sec_edgar"))
    source.permission_status = "approved"
    source.live_enabled = True
    source.fixture_only = False
    db_session.commit()
    monkeypatch.setattr(
        ocr_trial,
        "TRIAL_DOCUMENTS",
        (("AAPL", "0000320193", "0000320193-26-000099", "release.htm"),),
    )
    visited = []
    changed = False

    def respond(request):  # type: ignore[no-untyped-def]
        visited.append(str(request.url))
        prefix = "new" if changed else "page"
        if request.url.path.endswith("-index.html"):
            body = (
                '<table summary="Document Format Files">'
                + "".join(
                    f'<tr><td>2</td><td>Page</td><td><a href="{prefix}{i}.jpg">image</a></td>'
                    "<td>GRAPHIC</td></tr>"
                    for i in range(4)
                )
                + "</table>"
            )
            return httpx.Response(200, headers={"content-type": "text/html"}, text=body)
        if request.url.path.endswith(".htm"):
            body = "".join(f'<img src="{prefix}{i}.jpg">' for i in range(4))
            return httpx.Response(200, headers={"content-type": "text/html"}, text=body)
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=str(request.url).encode()
        )

    monkeypatch.setattr(
        ocr_trial,
        "SafeHttpClient",
        lambda policy: SafeHttpClient(
            replace(policy, min_interval_seconds=0), transport=httpx.MockTransport(respond)
        ),
    )
    cfg = Settings(
        output_dir=tmp_path / "out/publications",
        enable_live_sec=True,
        sec_user_agent="Offline Test qa@example.invalid",
    )
    first = ocr_trial.capture_trial(db_session, cfg)
    values = json.loads(first.read_text())
    assert not values["documents"][0]["complete_capture"]
    page = values["documents"][0]["pages"][0]
    if reuse_fault == "tampered":
        (first.parent / page["file"]).write_bytes(b"changed")
    if reuse_fault in {"stale", "future"}:
        for item in values["documents"][0]["pages"]:
            item["retrieved_at"] = (
                datetime.now(UTC) + timedelta(hours=-25 if reuse_fault == "stale" else 1)
            ).isoformat()
        first.write_text(json.dumps(values))
    changed = reuse_fault == "changed_index"
    visited.clear()
    if reuse_fault == "tampered":
        with pytest.raises(PayloadError, match="checksum"):
            ocr_trial.capture_trial(db_session, cfg, full_document=True, reuse_capture=first)
    else:
        result = ocr_trial.capture_trial(db_session, cfg, full_document=True, reuse_capture=first)
        complete = json.loads(result.read_text())
        assert complete["documents"][0]["complete_capture"]
        assert [item["page_number"] for item in complete["documents"][0]["pages"]] == [1, 2, 3, 4]
        assert complete["reused_images"] == (3 if reuse_fault is None else 0)
        assert len(visited) == (3 if reuse_fault is None else 6)
        if reuse_fault is None:
            assert complete["documents"][0]["pages"][0]["retrieved_at"] == page["retrieved_at"]
    assert visited[0].endswith("-index.html") and visited[1].endswith(".htm")
    assert db_session.scalar(select(func.count()).select_from(EventCurrent)) == 0
