"""Compare storage paths on disposable fixtures; never opens application data."""

from __future__ import annotations

import argparse
import gc
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.sqlite_store import SQLiteStore


def benchmark(base, size: int, repeats: int, ingest_count: int) -> dict:
    class MeasuredStore(base):
        connections = 0
        decodes = 0

        def _connect(self):
            self.connections += 1
            return super()._connect()

        def _loads(self, payload):
            self.decodes += 1
            return super()._loads(payload)

    with tempfile.TemporaryDirectory(prefix="content-bot-storage-benchmark-") as temporary:
        store = MeasuredStore(Path(temporary) / "fixture.db")
        store.initialize()
        now = datetime(2026, 9, 12, tzinfo=UTC)
        connection = store._connect()
        try:
            for identifier in range(1, size + 1):
                store._upsert_with(connection, "content_items", identifier, {
                    "_id": identifier, "source_id": "web", "external_id": str(identifier),
                    "title": "Synthetic item",
                })
                store._upsert_with(connection, "item_keyword_matches", identifier, {
                    "_id": identifier, "keyword_id": 1,
                    "content_item_id": identifier, "relevance_score": 80,
                })
            for collection in ("content_items", "item_keyword_matches"):
                connection.execute("INSERT INTO local_counters VALUES (?, ?) ON CONFLICT(collection) DO UPDATE SET value=excluded.value", (collection, size))
            connection.commit()
        finally:
            connection.close()

        results = {}
        try:
            for name, operation in (
                ("lookup_last", lambda: store.item_by_source("web", str(size))),
                ("item_matches", lambda: store.item_matches(1, positive_only=True)),
            ):
                timings = []
                for _ in range(repeats):
                    store.connections = store.decodes = 0
                    started = time.perf_counter()
                    operation()
                    timings.append((time.perf_counter() - started) * 1000)
                results[name] = {
                    "median_ms": round(statistics.median(timings), 3),
                    "connections": store.connections, "json_decodes": store.decodes,
                }

            # This measures persistence only, excluding RunManager/provider scoring.
            for phase in ("new", "duplicate"):
                if not ingest_count:
                    break
                started = time.perf_counter()
                item_timings = []
                for index in range(ingest_count):
                    item_started = time.perf_counter()
                    store.ingest_content_bundle(
                        {"source_id": "web", "external_id": f"added-{index}"},
                        {"captured_at": now + timedelta(seconds=index)},
                        {"relevance_score": 80, "trend_score": 50}, 1,
                    )
                    item_timings.append((time.perf_counter() - item_started) * 1000)
                elapsed = time.perf_counter() - started
                results[f"ingest_{phase}"] = {
                    "count": ingest_count, "seconds": round(elapsed, 3),
                    "items_per_second": round(ingest_count / elapsed, 1),
                    "p95_ms": round(sorted(item_timings)[max(0, (95 * ingest_count + 99) // 100 - 1)], 3),
                }
            connection = store._connect()
            try:
                counts = {collection: connection.execute("SELECT COUNT(*) FROM local_documents WHERE collection=?", (collection,)).fetchone()[0]
                          for collection in ("content_items", "item_keyword_matches", "metric_snapshots")}
                assert counts["content_items"] == size + ingest_count
                assert counts["item_keyword_matches"] == size + ingest_count
                results["verified_counts"] = counts
            finally:
                connection.close()
            return {"size": size, "repeats": repeats, "operations": results}
        finally:
            # Historical code relies on GC to close connections. Keep that behavior
            # inside the timed operations, but release handles before temp cleanup.
            gc.collect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 5000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--ingest-count", type=int, default=0)
    parser.add_argument("--baseline-revision", help="Optional Git commit hash to compare")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.sizes) < 1 or args.repeats < 1 or args.ingest_count < 0:
        parser.error("Sizes/repeats must be positive; ingest count cannot be negative")
    implementations = {"current": SQLiteStore}
    if args.baseline_revision:
        if not re.fullmatch(r"[a-fA-F0-9]{7,40}", args.baseline_revision):
            parser.error("Baseline must be a Git commit hash")
        source = subprocess.check_output(
            ["git", "show", f"{args.baseline_revision}:backend/app/sqlite_store.py"],
            cwd=ROOT, text=True, encoding="utf-8", timeout=30,
        )
        module = ModuleType("benchmark_baseline")
        # Explicit local Git revision selected by the operator, never remote input.
        exec(compile(source, "baseline_sqlite_store.py", "exec"), module.__dict__)  # noqa: S102
        implementations = {"baseline": module.SQLiteStore, **implementations}
    report = {"python": sys.version, "baseline_revision": args.baseline_revision, "results": []}
    for name, implementation in implementations.items():
        for size in args.sizes:
            result = {"implementation": name, **benchmark(implementation, size, args.repeats, args.ingest_count)}
            report["results"].append(result)
            print(json.dumps(result), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
