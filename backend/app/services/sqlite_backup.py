"""Consistent SQLite snapshots including committed WAL pages."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def backup_sqlite(source: Path, destination: Path) -> Path:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from the source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects any preexisting backup from being overwritten.
    with destination.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin,
            closing(sqlite3.connect(destination)) as target,
        ):
            origin.backup(target, pages=256)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise ValueError("SQLite backup integrity check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination
