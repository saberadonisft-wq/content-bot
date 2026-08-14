from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from bson import json_util

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings
from app.mongo import store


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Content Bot MongoDB backup.")
    parser.add_argument("--destination", required=True, type=Path, help="Destination JSON backup file")
    args = parser.parse_args()
    store.initialize()
    if not settings.mongodb_uri or not store.is_available:
        raise RuntimeError("MongoDB is not available; refusing to create an incomplete backup.")

    destination = args.destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    collections = {
        name: list(store.db[name].find())
        for name in store.db.list_collection_names()
    }
    payload = {
        "format": "content-bot-mongodb-v1",
        "database": settings.mongodb_database,
        "created_at": datetime.now(UTC),
        "collections": collections,
    }
    destination.write_text(json_util.dumps(payload, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "created_at": payload["created_at"].isoformat(),
                "database": settings.mongodb_database,
                "destination": str(destination),
                "collections": len(collections),
                "bytes": destination.stat().st_size,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
