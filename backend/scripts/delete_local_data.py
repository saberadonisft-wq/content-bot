from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from backup_sqlite import sqlite_path

from app.config import settings


def local_api_is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.25):
            return True
    except OSError:
        return False


def safe_data_directory() -> Path:
    data_dir = settings.data_dir.resolve()
    try:
        data_dir.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("Refusing to delete a data directory outside this workspace.") from exc
    if data_dir == PROJECT_ROOT:
        raise ValueError("Refusing to delete the workspace root as a data directory.")

    database_path = sqlite_path(settings.content_bot_database_url).resolve()
    try:
        database_path.relative_to(data_dir)
    except ValueError as exc:
        raise ValueError("Refusing to delete because the configured SQLite database is outside the data directory.") from exc
    return data_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Permanently delete all Content Bot local data in the configured workspace data directory."
    )
    parser.add_argument(
        "--confirm-delete-local-data",
        action="store_true",
        help="Required acknowledgement that collected records, backups and browser profiles will be deleted.",
    )
    args = parser.parse_args()
    if not args.confirm_delete_local_data:
        raise PermissionError("Pass --confirm-delete-local-data to permanently delete local data.")
    if local_api_is_running():
        raise RuntimeError("The local API is running on port 8000. Stop it before deleting local data.")

    data_dir = safe_data_directory()
    existed = data_dir.exists()
    if existed:
        shutil.rmtree(data_dir)
    print(json.dumps({"deleted": existed, "data_directory": str(data_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
