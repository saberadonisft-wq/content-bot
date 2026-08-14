from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    parser = argparse.ArgumentParser(description="Preview or remove expired Content Bot MongoDB content.")
    parser.add_argument("--days", required=True, type=int, help="Keep items seen within this many days (1-3650).")
    parser.add_argument("--apply", action="store_true", help="Delete instead of only reporting the count.")
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        raise ValueError("--days must be between 1 and 3650")
    store.initialize()
    if not settings.mongodb_uri or not store.is_available:
        raise RuntimeError("MongoDB is not available; refusing to prune data.")
    if args.apply and local_api_is_running():
        raise RuntimeError("The local API is running. Stop it before pruning MongoDB data.")

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    expired_ids = [
        row["_id"]
        for row in store.db.content_items.find(
            {"last_seen_at": {"$lt": cutoff}},
            {"_id": 1},
        )
    ]
    deleted = 0
    if args.apply and expired_ids:
        for content_item_id in expired_ids:
            store.delete_item(content_item_id)
            deleted += 1

    print(
        json.dumps(
            {
                "mode": "applied" if args.apply else "preview",
                "retention_days": args.days,
                "cutoff": cutoff.isoformat(),
                "matching_items": len(expired_ids),
                "deleted_items": deleted,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
