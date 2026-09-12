"""Measure isolated app readiness and bounded job history recovery/shutdown."""

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


async def measure(directory: Path, history: int):
    from app.application_services import AppServices
    from app.config import settings
    from app.main import create_app
    from app.services.subtitle_jobs import SubtitleJobRecord
    from app.sqlite_store import SQLiteStore

    settings.content_bot_data_dir = directory
    # Settings.data_dir is resolved from the configurable path in this isolated process.
    job_dir = settings.data_dir / "subtitle-jobs"
    job_dir.mkdir(parents=True, exist_ok=True)
    for i in range(history):
        record = SubtitleJobRecord(
            id=f"{i:020x}",
            kind="alignment",
            dedupe_key=str(i),
            state="succeeded",
            result={"segments": []},
        )
        (job_dir / f"{record.id}.json").write_text(
            json.dumps(record.snapshot()), encoding="utf-8"
        )
    interrupted = []
    for i, state in enumerate(("queued", "running")):
        record = SubtitleJobRecord(
            id=f"{history + i:020x}",
            kind="alignment",
            dedupe_key=f"interrupted-{i}",
            state=state,
        )
        (job_dir / f"{record.id}.json").write_text(
            json.dumps(record.snapshot()), encoding="utf-8"
        )
        interrupted.append(record.id)
    results = []
    for iteration in range(3):
        created = {}

        def services_factory(timings=created):
            start = time.perf_counter()
            services = AppServices.create(storage=SQLiteStore(directory / "fixture.db"))
            timings["manager_creation_ms"] = (time.perf_counter() - start) * 1000
            return services

        app = create_app(services_factory, scheduler=False)
        start = time.perf_counter()
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        ready_ms = (time.perf_counter() - start) * 1000
        services = app.state.services
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            response = await client.get("/api/v1/ready")
            response.raise_for_status()
        for identifier in interrupted:
            assert services.subtitle_jobs.get(identifier)["phase"] == "interrupted"
        records = len(services.subtitle_jobs._records)
        assert records <= 128 and not services.subtitle_jobs._futures
        start = time.perf_counter()
        await context.__aexit__(None, None, None)
        shutdown_ms = (time.perf_counter() - start) * 1000
        results.append(
            {
                "iteration": iteration,
                "ready_ms": ready_ms,
                "shutdown_ms": shutdown_ms,
                "cached_records": records,
                **created,
            }
        )
    return {"completed_history": history, "interrupted_records": 2, "samples": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--histories", nargs="+", type=int, default=[0, 1000, 10000])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="content-bot-lifecycle-") as temporary:
        os.environ.update(
            {
                "CONTENT_BOT_DATA_DIR": temporary,
                "CONTENT_BOT_SQLITE_PATH": str(Path(temporary) / "unused.db"),
                "CONTENT_BOT_STORAGE_BACKEND": "sqlite",
                "CONTENT_BOT_AUTH_ENABLED": "false",
            }
        )
        start = time.perf_counter()
        __import__("app.main")
        import_ms = (time.perf_counter() - start) * 1000
        assert not list(Path(temporary).rglob("*.json")) and not list(
            Path(temporary).rglob("*.db")
        )
        results = []
        for history in args.histories:
            directory = Path(temporary) / str(history)
            directory.mkdir()
            results.append(asyncio.run(measure(directory, history)))
            print(json.dumps(results[-1]), flush=True)
        report = {
            "python": sys.version,
            "platform": platform.platform(),
            "import_ms": import_ms,
            "import_created_files": False,
            "scope": "local lifespan, scheduler disabled, queued/running history recovered; no inference/network jobs",
            "results": results,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
