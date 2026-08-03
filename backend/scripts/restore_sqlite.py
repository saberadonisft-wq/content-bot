from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from backup_sqlite import sqlite_path

from app.config import settings


def integrity_check(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result is None or result[0] != "ok":
        raise ValueError(f"Backup integrity check failed: {result[0] if result else 'no result'}")


def local_api_is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.25):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore a verified Content Bot SQLite backup using an atomic replacement."
    )
    parser.add_argument("--source", required=True, type=Path, help="Existing backup .db file")
    parser.add_argument(
        "--replace-current",
        action="store_true",
        help="Required acknowledgement that the current configured database will be replaced.",
    )
    args = parser.parse_args()

    source_path = args.source.resolve()
    target_path = sqlite_path(settings.content_bot_database_url).resolve()
    if not args.replace_current:
        raise PermissionError("Pass --replace-current to restore over the configured database.")
    if local_api_is_running():
        raise RuntimeError("The local API is running on port 8000. Stop it before restoring.")
    if not source_path.is_file():
        raise FileNotFoundError(f"Backup file was not found: {source_path}")
    if source_path == target_path:
        raise ValueError("The backup source cannot be the current configured database.")

    integrity_check(source_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.restore")
    source = sqlite3.connect(source_path)
    staging = sqlite3.connect(staging_path)
    try:
        source.backup(staging)
    finally:
        staging.close()
        source.close()

    try:
        integrity_check(staging_path)
        os.replace(staging_path, target_path)
    finally:
        if staging_path.exists():
            staging_path.unlink()

    print(
        json.dumps(
            {
                "restored_at": datetime.now(UTC).isoformat(),
                "source": str(source_path),
                "destination": str(target_path),
                "bytes": target_path.stat().st_size,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
