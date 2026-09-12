"""Compare full local ingestion/scoring on disposable 100k-item SQLite fixtures.

The legacy comparator changes only source-wide snapshot reads back to one query
per item. Both modes use current persistence/analysis, not the old Git storage.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def measure(directory: Path, mode: str, size: int, count: int):
    from app.services import runs
    from app.services.connector_contracts import RawContentItem
    from app.services.text import recency_score
    from app.sqlite_store import SQLiteStore

    class MeasuredStore(SQLiteStore):
        statements = 0
        connections = 0

        def _connect(self):
            self.connections += 1
            connection = super()._connect()
            connection.set_trace_callback(self.trace)
            return connection

        def trace(self, _sql):
            self.statements += 1

        def source_snapshot_pairs(self, source_id):
            if mode == "bulk":
                return super().source_snapshot_pairs(source_id)
            pairs = (
                self.snapshots(identifier, descending=True, limit=2)
                for identifier in self.source_item_ids(source_id)
            )
            return [pair for pair in pairs if len(pair) == 2]

    store = MeasuredStore(directory / f"{mode}.db")
    store.initialize()
    now = datetime(2026, 9, 12, 10, tzinfo=UTC)
    with store._connection() as connection:
        connection.execute("BEGIN")
        # Raw fixture inserts avoid timing fixture preparation as ingestion.
        connection.executemany(
            "INSERT INTO local_documents VALUES (?,?,?)",
            (
                (
                    "content_items",
                    f"i:{i}",
                    store._dumps(
                        {
                            "_id": i,
                            "source_id": f"source-{i % 10}",
                            "external_id": str(i),
                            "title": "Fixture game",
                            "metrics": {"like_count": i % 100},
                            "first_seen_at": now,
                            "last_seen_at": now,
                            "published_at": now,
                        }
                    ),
                )
                for i in range(1, size + 1)
            ),
        )
        connection.execute(
            "INSERT INTO local_counters VALUES ('content_items', ?)", (size,)
        )
        connection.commit()
    store.create_batch(
        {"id": "fixture", "keyword_id": 1, "state": "running", "started_at": now},
        [
            {
                "id": f"run-{i}",
                "batch_id": "fixture",
                "source_id": f"source-{i}",
                "state": "running",
                "started_at": now,
                "fetched_count": 0,
                "ingested_count": 0,
                "progress_current": 0,
            }
            for i in range(10)
        ],
    )
    manager = runs.RunManager({}, runs.EventBus(), store)
    original_now, original_recency = runs.utcnow, runs.recency_score
    results = []
    try:
        runs.recency_score = lambda published: recency_score(published, now=now)
        for phase in ("new", "duplicate"):
            observed = now + (
                timedelta(hours=1) if phase == "duplicate" else timedelta()
            )
            runs.utcnow = lambda value=observed: value
            timings = []
            store.statements = store.connections = 0
            started = time.perf_counter()
            for i in range(count):
                raw = RawContentItem(
                    external_id=f"new-{i}",
                    canonical_url=f"https://example.test/{i}",
                    title="Fixture game",
                    published_at=now,
                    metrics={
                        "like_count": i % 100 + (10 if phase == "duplicate" else 0)
                    },
                )
                begin = time.perf_counter()
                added = manager._ingest_sync(f"run-{i % 10}", 1, ["game"], [], raw)
                assert added == (phase == "new")
                timings.append((time.perf_counter() - begin) * 1000)
                if (i + 1) % 100 == 0:
                    print(
                        json.dumps(
                            {
                                "mode": mode,
                                "phase": phase,
                                "done": i + 1,
                                "elapsed_seconds": round(
                                    time.perf_counter() - started, 2
                                ),
                            }
                        ),
                        flush=True,
                    )
            elapsed = time.perf_counter() - started
            results.append(
                {
                    "phase": phase,
                    "count": count,
                    "seconds": elapsed,
                    "items_per_second": count / elapsed,
                    "p50_ms": statistics.median(timings),
                    "p95_ms": sorted(timings)[math_index(count)],
                    "statements": store.statements,
                    "connections": store.connections,
                }
            )
        with store._connection() as connection:
            counts = {
                name: connection.execute(
                    "SELECT COUNT(*) FROM local_documents WHERE collection=?", (name,)
                ).fetchone()[0]
                for name in (
                    "content_items",
                    "item_keyword_matches",
                    "metric_snapshots",
                )
            }
        assert counts == {
            "content_items": size + count,
            "item_keyword_matches": count,
            "metric_snapshots": count * 2,
        }, counts
        assert (
            sum(row["ingested_count"] for row in store.source_runs("fixture")) == count
        )
        assert (
            sum(row["fetched_count"] for row in store.source_runs("fixture"))
            == count * 2
        )
        scores = sorted(
            (row["content_item_id"], row["trend_score"])
            for row in store._all("item_keyword_matches")
        )
        return {
            "mode": mode,
            "results": results,
            "counts": counts,
            "score_sha256": hashlib.sha256(json.dumps(scores).encode()).hexdigest(),
        }
    finally:
        runs.utcnow, runs.recency_score = original_now, original_recency
        asyncio.run(manager.shutdown())


def math_index(count):
    return max(0, (95 * count + 99) // 100 - 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=100_000)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument(
        "--modes", nargs="+", choices=["bulk", "legacy"], default=["bulk", "legacy"]
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.size < 10 or args.count < 1:
        parser.error("Use at least 10 existing items and 1 ingested item")
    with tempfile.TemporaryDirectory(prefix="content-bot-ingestion-") as temporary:
        directory = Path(temporary)
        os.environ.update(
            {
                "CONTENT_BOT_DATA_DIR": temporary,
                "CONTENT_BOT_SQLITE_PATH": str(directory / "unused.db"),
                "CONTENT_BOT_STORAGE_BACKEND": "sqlite",
                "CONTENT_BOT_AUTH_ENABLED": "false",
            }
        )
        report = {
            "python": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
            "existing_items": args.size,
            "sources": 10,
            "scope": "RunManager._ingest_sync: relevance, scoring, persistence and counters; excludes provider/network/scheduler",
            "results": [],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        for mode in args.modes:
            report["results"].append(measure(directory, mode, args.size, args.count))
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        assert len({result["score_sha256"] for result in report["results"]}) == 1
    print(json.dumps(report))


if __name__ == "__main__":
    main()
