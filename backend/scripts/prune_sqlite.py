from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings
from app.database import SessionLocal
from app.models import ContentItem


def local_api_is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.25):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or permanently remove Content Bot items older than a retention period."
    )
    parser.add_argument("--days", required=True, type=int, help="Keep items seen within this many days (1-3650).")
    parser.add_argument("--apply", action="store_true", help="Delete instead of only reporting the matching count.")
    args = parser.parse_args()
    if settings.mongodb_uri:
        raise RuntimeError(
            "MongoDB is the active persistence backend. prune_sqlite.py is legacy-only and will not change runtime data."
        )
    if not 1 <= args.days <= 3650:
        raise ValueError("--days must be between 1 and 3650")
    if args.apply and local_api_is_running():
        raise RuntimeError("The local API is running on port 8000. Stop it before pruning data.")

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    with SessionLocal() as session:
        rows = session.scalars(select(ContentItem).where(ContentItem.last_seen_at < cutoff)).all()
        count = len(rows)
        if args.apply:
            for row in rows:
                session.delete(row)
            session.commit()
        total = session.scalar(select(func.count()).select_from(ContentItem)) or 0

    print(
        json.dumps(
            {
                "mode": "applied" if args.apply else "preview",
                "retention_days": args.days,
                "cutoff": cutoff.isoformat(),
                "matching_items": count,
                "remaining_items": total,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
