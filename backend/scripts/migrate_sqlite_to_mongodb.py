from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.mongo import store
from app.services.text import normalized

COLLECTIONS = (
    "keywords",
    "content_items",
    "crawl_batches",
    "item_keyword_matches",
    "metric_snapshots",
    "source_runs",
)

DATE_FIELDS = {
    "keywords": {"next_run_at", "created_at", "updated_at"},
    "content_items": {"published_at", "first_seen_at", "last_seen_at"},
    "crawl_batches": {"started_at", "finished_at"},
    "item_keyword_matches": {"updated_at"},
    "metric_snapshots": {"captured_at"},
    "source_runs": {"started_at", "finished_at"},
}

JSON_FIELDS = {
    "keywords": {
        "include_terms_json": "include_terms",
        "exclude_terms_json": "exclude_terms",
        "source_ids_json": "source_ids",
    },
    "content_items": {
        "hashtags_json": "hashtags",
        "metrics_json": "metrics",
        "raw_payload_json": "raw_payload",
    },
    "item_keyword_matches": {"match_reasons_json": "match_reasons"},
    "source_runs": {"checkpoint_json": "checkpoint"},
}


def sqlite_path() -> Path:
    prefix = "sqlite:///"
    if not settings.content_bot_database_url.startswith(prefix):
        raise RuntimeError("CONTENT_BOT_DATABASE_URL must point to the source SQLite database")
    path = Path(settings.content_bot_database_url[len(prefix) :])
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path.resolve()


def parse_datetime(value):
    if not value or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def transform(table: str, raw: sqlite3.Row) -> dict:
    row = dict(raw)
    row["_id"] = row.pop("id")
    for field in DATE_FIELDS.get(table, set()):
        row[field] = parse_datetime(row.get(field))
    for source, target in JSON_FIELDS.get(table, {}).items():
        value = row.pop(source)
        fallback = "{}" if target in {"metrics", "raw_payload", "checkpoint"} else "[]"
        row[target] = json.loads(value or fallback)
    if table == "keywords":
        row["normalized_name"] = normalized(row["name"])
        row["enabled"] = bool(row["enabled"])
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy the configured SQLite data into MongoDB Atlas")
    parser.add_argument("--replace", action="store_true", help="Replace existing documents with the same IDs")
    args = parser.parse_args()
    source = sqlite_path()
    if not source.is_file():
        raise FileNotFoundError(source)
    store.initialize()
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    migrated: dict[str, int] = {}
    try:
        for table in COLLECTIONS:
            rows = [transform(table, row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            for row in rows:
                if args.replace:
                    store.db[table].replace_one({"_id": row["_id"]}, row, upsert=True)
                else:
                    store.db[table].update_one({"_id": row["_id"]}, {"$setOnInsert": row}, upsert=True)
            migrated[table] = len(rows)
            numeric_ids = [row["_id"] for row in rows if isinstance(row["_id"], int)]
            if numeric_ids:
                store.db.counters.update_one(
                    {"_id": table}, {"$max": {"value": max(numeric_ids)}}, upsert=True
                )
    finally:
        connection.close()
    print(json.dumps({"database": store.db.name, "migrated": migrated}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
