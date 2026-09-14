"""Bound cache retention without deleting data owned by an active pipeline."""
from __future__ import annotations

import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_lock = threading.Lock()
_active: dict[Path, int] = {}


def prune_cache(root: Path, *, retention_days: int, max_bytes: int) -> None:
    root = root.resolve()
    cutoff = time.time() - retention_days * 86400
    entries = []
    for parent in (root / "checkpoints", root / "media-cache"):
        if not parent.is_dir() or parent.is_symlink():
            continue
        for directory in parent.iterdir():
            if not re.fullmatch(r"[0-9a-f]{64}", directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            resolved = directory.resolve()
            if not resolved.is_relative_to(root) or resolved.parent != parent.resolve():
                continue
            files = [path for path in directory.rglob("*") if path.is_file() and not path.is_symlink()]
            modified = max([directory.stat().st_mtime] + [path.stat().st_mtime for path in files])
            entries.append((modified, sum(path.stat().st_size for path in files), resolved))
    total = sum(size for _, size, _ in entries)
    for modified, size, directory in sorted(entries):
        if modified >= cutoff and total <= max_bytes:
            continue
        try:
            shutil.rmtree(directory)
            total -= size
        except OSError:
            continue


@contextmanager
def cache_session(root: Path, *, retention_days: int, max_bytes: int):
    root = root.resolve()
    with _lock:
        if not _active.get(root):
            prune_cache(root, retention_days=retention_days, max_bytes=max_bytes)
        _active[root] = _active.get(root, 0) + 1
    try:
        yield
    finally:
        with _lock:
            _active[root] -= 1
            if not _active[root]:
                del _active[root]
