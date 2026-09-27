"""Atomic persistence survives transient Windows reader locks without hiding failures."""

import errno
import json
from pathlib import Path

import pytest

from app.services import gemini_media


def test_transient_windows_replace_lock_keeps_old_json_until_success(
    tmp_path, monkeypatch
):
    target = tmp_path / "progress.json"
    target.write_text('{"progress": 1}', encoding="utf-8")
    replace = Path.replace
    attempts, waits = [], []

    def locked(path, destination):
        attempts.append(path)
        if len(attempts) < 3:
            assert json.loads(target.read_text()) == {"progress": 1}
            error = PermissionError("fixture sharing violation")
            error.winerror = 32
            raise error
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr(gemini_media.time, "sleep", waits.append)
    gemini_media.atomic_json(target, {"progress": 2})
    assert json.loads(target.read_text()) == {"progress": 2}
    assert waits == [0.05, 0.05]
    assert len(set(attempts)) == 1
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "kind", ["permanent_windows_permission", "disk_full", "posix_permission"]
)
def test_persistence_failure_is_bounded_and_preserves_previous_file(
    tmp_path, monkeypatch, kind
):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"before": true}', encoding="utf-8")
    waits = []
    error = (
        OSError(errno.ENOSPC, "fixture disk full")
        if kind == "disk_full"
        else PermissionError("fixture permission")
    )
    if kind == "permanent_windows_permission":
        error.winerror = 5

    def failed(*args):
        raise error

    monkeypatch.setattr(Path, "replace", failed)
    monkeypatch.setattr(gemini_media.time, "sleep", waits.append)
    with pytest.raises(OSError) as caught:
        gemini_media.atomic_json(target, {"after": True})
    assert caught.value is error
    assert len(waits) == (10 if kind == "permanent_windows_permission" else 0)
    assert json.loads(target.read_text()) == {"before": True}
    assert not list(tmp_path.glob("*.tmp"))
