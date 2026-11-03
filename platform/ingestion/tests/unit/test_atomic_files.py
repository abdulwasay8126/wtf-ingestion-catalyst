from pathlib import Path

import pytest

from wtf_ingestion.publication import files


def test_transient_sharing_lock_retries_without_removing_last_good(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    destination = tmp_path / "status.json"
    temporary = tmp_path / ".status.tmp"
    destination.write_text("last good")
    temporary.write_text("new status")
    original = files.os.replace
    attempts = []
    waits = []

    def replace(source: Path, target: Path) -> None:
        attempts.append(target)
        assert destination.read_text() == "last good"
        if len(attempts) < 3:
            raise PermissionError("simulated Windows sharing lock")
        original(source, target)

    monkeypatch.setattr(files.os, "replace", replace)
    monkeypatch.setattr(files.time, "sleep", waits.append)
    files.replace_atomic(temporary, destination)
    assert destination.read_text() == "new status"
    assert len(attempts) == 3 and waits == [0.1, 0.2]


def test_persistent_permission_failure_preserves_both_files(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    destination = tmp_path / "current.json"
    temporary = tmp_path / ".current.tmp"
    destination.write_text("last good")
    temporary.write_text("pending")
    attempts = []
    waits = []

    def replace(source: Path, target: Path) -> None:
        attempts.append(target)
        raise PermissionError("persistent permission failure")

    monkeypatch.setattr(files.os, "replace", replace)
    monkeypatch.setattr(files.time, "sleep", waits.append)
    with pytest.raises(PermissionError):
        files.replace_atomic(temporary, destination)
    assert len(attempts) == 6 and sum(waits) == pytest.approx(3.1)
    assert destination.read_text() == "last good" and temporary.read_text() == "pending"


def test_other_io_errors_are_not_hidden_or_retried(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    def replace(source: Path, target: Path) -> None:
        raise OSError("disk error")

    monkeypatch.setattr(files.os, "replace", replace)
    monkeypatch.setattr(files.time, "sleep", lambda _: pytest.fail("no retry for disk errors"))
    with pytest.raises(OSError, match="disk error"):
        files.replace_atomic(tmp_path / "pending", tmp_path / "target")
