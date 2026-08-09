from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings


def sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("Only sqlite:/// database URLs can be backed up by this v1 script.")
    raw_path = Path(database_url.removeprefix(prefix))
    return raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a consistent Content Bot SQLite backup.")
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()

    if settings.mongodb_uri:
        raise RuntimeError(
            "MongoDB is the active persistence backend. backup_sqlite.py is legacy-only; use mongodump for the configured MongoDB database."
        )
    source_path = sqlite_path(settings.content_bot_database_url).resolve()
    destination = args.destination.resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"SQLite database was not found: {source_path}")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    print(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "source": str(source_path),
                "destination": str(destination),
                "bytes": destination.stat().st_size,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
