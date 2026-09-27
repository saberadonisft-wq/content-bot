"""Cache limits protect in-use checkpoints, survive worker death and stay scoped."""

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.subtitle_cache import CacheSession


def entry(root, number):
    return root / f"batch-{number:064x}.json"


def test_lru_byte_budget_evicts_cold_entry_but_keeps_recent_read(tmp_path):
    value = {"text": "x" * 40}
    size = len(json.dumps(value).encode())
    with CacheSession(tmp_path, max_bytes=size * 2) as session:
        session.write(entry(tmp_path, 1), value)
        session.write(entry(tmp_path, 2), value)
    os.utime(entry(tmp_path, 1), (1, 1))
    os.utime(entry(tmp_path, 2), (2, 2))
    with CacheSession(tmp_path, max_bytes=size * 2) as session:
        assert session.read(entry(tmp_path, 1)) == value
        session.write(entry(tmp_path, 3), value)
    assert entry(tmp_path, 1).exists()
    assert not entry(tmp_path, 2).exists()
    assert entry(tmp_path, 3).exists()
    assert sum(p.stat().st_size for p in tmp_path.glob("batch-*.json")) == size * 2


def test_active_job_checkpoints_are_pinned_and_quota_failure_is_explicit(tmp_path):
    with CacheSession(tmp_path, max_entries=1) as active:
        active.write(entry(tmp_path, 1), {"translation": "completed"})
        with CacheSession(tmp_path, max_entries=1) as other:
            with pytest.raises(OSError) as caught:
                other.write(entry(tmp_path, 2), {"translation": "another"})
            assert caught.value.errno == errno.ENOSPC
        assert active.read(entry(tmp_path, 1)) == {"translation": "completed"}
    with CacheSession(tmp_path, max_entries=1) as later:
        later.write(entry(tmp_path, 2), {"translation": "another"})
    assert not entry(tmp_path, 1).exists()


def test_replacement_counts_final_bytes_and_not_an_extra_entry(tmp_path):
    with CacheSession(tmp_path, max_bytes=50, max_entries=1) as session:
        session.write(entry(tmp_path, 1), {"text": "old"})
        session.write(entry(tmp_path, 1), {"text": "new"})
        assert session.read(entry(tmp_path, 1)) == {"text": "new"}


def test_oversized_result_does_not_destroy_existing_cache(tmp_path):
    with CacheSession(tmp_path, max_bytes=40) as session:
        session.write(entry(tmp_path, 1), {"text": "old"})
        with pytest.raises(OSError):
            session.write(entry(tmp_path, 2), {"text": "x" * 41})
        assert session.read(entry(tmp_path, 1)) == {"text": "old"}


def test_cleanup_does_not_touch_versions_unknown_files_or_nested_directories(tmp_path):
    original = tmp_path / "original.json"
    original.write_text("original")
    nested = tmp_path / "versions"
    nested.mkdir()
    version = entry(nested, 9)
    version.write_text("version")
    abandoned = entry(tmp_path, 3).with_suffix("." + "c" * 32 + ".tmp")
    abandoned.write_text("interrupted JSON")
    unknown_temporary = tmp_path / "unrelated.tmp"
    unknown_temporary.write_text("keep this")
    with CacheSession(tmp_path, max_entries=1) as session:
        with pytest.raises(OSError):
            session.write(original, {})
        with pytest.raises(OSError):
            session.write(version, {})
        session.write(entry(tmp_path, 1), {})
    with CacheSession(tmp_path, max_entries=1) as session:
        session.write(entry(tmp_path, 2), {})
    assert original.read_text() == "original"
    assert version.read_text() == "version"
    assert not abandoned.exists()
    assert unknown_temporary.read_text() == "keep this"


def test_live_process_pins_are_released_by_os_after_termination(tmp_path):
    script = """
import sys,time
from pathlib import Path
from app.services.subtitle_cache import CacheSession
root=Path(sys.argv[1])
with CacheSession(root,max_entries=1) as session:
    session.write(root/('batch-'+format(1,'064x')+'.json'),{'translation':'worker'})
    print('pinned',flush=True)
    time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert process.stdout.readline().strip() == "pinned"
        with CacheSession(tmp_path, max_entries=1) as parent, pytest.raises(OSError):
            parent.write(entry(tmp_path, 2), {})
        process.kill()
        process.communicate(timeout=5)
        with CacheSession(tmp_path, max_entries=1) as parent:
            parent.write(entry(tmp_path, 2), {})
        assert not entry(tmp_path, 1).exists()
        assert entry(tmp_path, 2).exists()
        assert not list(tmp_path.glob(".session-*.lock"))
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
