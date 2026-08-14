from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from bson import json_util

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings
from app.mongo import store


def local_api_is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", settings.content_bot_port), timeout=0.25):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a verified Content Bot MongoDB backup.")
    parser.add_argument("--source", required=True, type=Path, help="Existing JSON backup file")
    parser.add_argument(
        "--replace-current",
        action="store_true",
        help="Required acknowledgement that the current database will be replaced.",
    )
    args = parser.parse_args()
    store.initialize()
    if not settings.mongodb_uri or not store.is_available:
        raise RuntimeError("MongoDB is not available; refusing to restore data.")
    if not args.replace_current:
        raise PermissionError("Pass --replace-current to restore over the configured database.")
    if local_api_is_running():
        raise RuntimeError("The local API is running. Stop it before restoring MongoDB data.")

    source = args.source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Backup file was not found: {source}")
    payload = json_util.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != "content-bot-mongodb-v1":
        raise ValueError("Unsupported or invalid MongoDB backup format")
    if payload.get("database") != settings.mongodb_database:
        raise ValueError("Backup database does not match the configured MongoDB database")
    collections = payload.get("collections")
    if not isinstance(collections, dict):
        raise TypeError("Backup collections are invalid")

    for name, documents in collections.items():
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("system.")
            or not isinstance(documents, list)
        ):
            raise ValueError("Backup contains an invalid collection")
        collection = store.db[name]
        collection.delete_many({})
        if documents:
            collection.insert_many(documents)

    print(json.dumps({"restored": True, "database": settings.mongodb_database, "collections": len(collections)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
