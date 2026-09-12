"""Measure the actual /items route on an isolated SQLite fixture, without starting jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


async def benchmark(directory: Path, size: int, repeats: int):
    os.environ["CONTENT_BOT_DATA_DIR"] = str(directory)
    os.environ["CONTENT_BOT_SQLITE_PATH"] = str(directory / "fixture.db")
    os.environ["CONTENT_BOT_STORAGE_BACKEND"] = "sqlite"
    os.environ["CONTENT_BOT_AUTH_ENABLED"] = "false"
    from app import main
    from app.application_services import AppServices
    from app.sqlite_store import SQLiteStore

    class MeasuredStore(SQLiteStore):
        connections = 0
        decodes = 0

        def _connect(self):
            self.connections += 1
            return super()._connect()

        def _loads(self, payload):
            self.decodes += 1
            return super()._loads(payload)

    store = MeasuredStore(directory / "fixture.db")
    store.initialize()
    now = datetime(2026, 9, 12, tzinfo=UTC)
    with store._transaction() as connection:
        for index in range(1, size + 1):
            store._upsert_with(connection, "content_items", index, {
                "_id": index, "source_id": "web", "external_id": str(index),
                "title": "Amazing patch release" if index % 3 else "Terrible crash bug",
                "canonical_url": f"https://example.com/{index}", "locale": "en",
                "first_seen_at": now, "last_seen_at": now, "published_at": now,
            })
            store._upsert_with(connection, "item_keyword_matches", index, {
                "_id": index, "content_item_id": index, "keyword_id": index % 10 + 1,
                "relevance_score": 80, "trend_score": index % 100,
            })
    application = main.create_app(lambda: AppServices.create(storage=store), scheduler=False)
    results = []
    # ASGI transport invokes the production middleware/router/serialization.
    # Start/stop the isolated services; the scheduler is disabled and no jobs run.
    async with application.router.lifespan_context(application), httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://benchmark") as client:
        for indexed in (False, True):
            main.settings.content_bot_indexed_item_queries = indexed
            for filters in ({}, {"topic": "bugs"}, {"language": "en", "sentiment": "positive"}):
                timings = []
                for _ in range(repeats + 1):
                    store.connections = store.decodes = 0
                    start = time.perf_counter()
                    response = await client.get("/api/v1/items", params={"keyword_id": 1, "limit": 50, **filters})
                    response.raise_for_status()
                    timings.append((time.perf_counter() - start) * 1000)
                warm = sorted(timings[1:])
                result = {"indexed": indexed, "filters": filters, "total": response.json()["total"],
                          "first_request_ms": round(timings[0], 3), "warm_samples": repeats,
                          "warm_p50_ms": round(statistics.median(warm), 3),
                          "warm_p95_ms": round(warm[max(0, (95 * repeats + 99) // 100 - 1)], 3),
                          "connections": store.connections, "document_decodes": store.decodes}
                print(json.dumps(result), flush=True)
                results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.size < 10 or args.repeats < 30:
        parser.error("Use at least 10 items and 30 warm samples")
    with tempfile.TemporaryDirectory(prefix="content-bot-item-api-") as directory:
        results = asyncio.run(benchmark(Path(directory), args.size, args.repeats))
    report = {"python": sys.version, "platform": platform.platform(), "processor": platform.processor(),
              "dataset_items": args.size, "keywords": 10, "page_size": 50,
              "transport": "in-process ASGI, isolated lifespan without scheduler/jobs, no server/network", "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
