from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from app import application_services as service_module
from app.application_services import AppServices
from app.config import settings
from app.main import create_app
from app.services.http_pool import get_client, use_pool_scope
from app.services.runs import EventBus, RunManager
from app.sqlite_store import SQLiteStore


def test_import_and_openapi_do_not_recover_jobs_or_create_database(tmp_path):
    subtitle = tmp_path / "subtitle-jobs" / ("a" * 20 + ".json")
    voice = tmp_path / "voiceover" / "owner" / "jobs" / ("b" * 20 + ".json")
    subtitle.parent.mkdir(parents=True)
    voice.parent.mkdir(parents=True)
    subtitle.write_text(json.dumps({"id": "a" * 20, "kind": "render", "dedupe_key": "old", "state": "running"}), encoding="utf-8")
    voice.write_text(json.dumps({"id": "b" * 20, "state": "queued"}), encoding="utf-8")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    code = """
import json
from app.main import app, create_app
assert app.state.services is None
schema = create_app().openapi()
assert '/api/v1/items' in schema['paths']
assert '/api/v1/voiceover/references' in schema['paths']
assert '/api/v1/keywords' in schema['paths']
assert '/api/v1/runs/{batch_id}/events' in schema['paths']
for path in schema['paths'].values():
    for operation in path.values():
        if isinstance(operation, dict):
            assert all(parameter['name'] != 'services' for parameter in operation.get('parameters', []))
print(json.dumps({'paths': len(schema['paths'])}))
"""
    env = {**os.environ, "CONTENT_BOT_DATA_DIR": str(tmp_path), "CONTENT_BOT_SQLITE_PATH": str(tmp_path / "unused.db"), "CONTENT_BOT_STORAGE_BACKEND": "sqlite"}
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1], env=env,
                            capture_output=True, text=True, check=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert json.loads(result.stdout)["paths"] > 50
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_factories_own_separate_services_and_http_pools_and_support_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path)
    first = create_app(lambda: AppServices.create(storage=SQLiteStore(tmp_path / "one.db")), scheduler=False)
    second = create_app(lambda: AppServices.create(storage=SQLiteStore(tmp_path / "two.db")), scheduler=False)
    route_count = len(first.routes)

    async def run():
        async with second.router.lifespan_context(second):
            async with first.router.lifespan_context(first):
                old_services = first.state.services
                assert old_services is not second.state.services
                assert old_services.voiceover_manager is not second.state.services.voiceover_manager
                transport = httpx.MockTransport(lambda request: httpx.Response(200))
                with use_pool_scope(first.state.http_pool_scope):
                    one = await get_client(transport=transport)
                with use_pool_scope(second.state.http_pool_scope):
                    two = await get_client(transport=transport)
                assert one is not two
            assert first.state.services is None and one.is_closed
            assert not two.is_closed
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://test") as client:
                assert (await client.get("/api/v1/keywords")).status_code == 200
            async with first.router.lifespan_context(first):
                assert first.state.services is not old_services
                assert first.state.services.subtitle_jobs._accepting
                assert len(first.routes) == route_count
        assert two.is_closed and second.state.services is None

    asyncio.run(run())


def test_startup_failure_closes_constructed_services(application_services, monkeypatch):
    async def fail(**kwargs):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(application_services, "start", fail)
    app = create_app(lambda: application_services)

    async def run():
        with pytest.raises(RuntimeError, match="startup failed"):
            async with app.router.lifespan_context(app):
                pytest.fail("Startup failure must not accept requests")
        assert app.state.services is None
        assert application_services._closed
        assert not application_services.subtitle_jobs._accepting
        assert not application_services.voiceover_manager._accepting

    asyncio.run(run())


def test_shutdown_stops_scheduler_then_jobs_before_http_pool(application_services, monkeypatch):
    order = []

    async def pool_close():
        order.append("http")

    monkeypatch.setattr(service_module, "close_http_pools", pool_close)
    for label, manager in (("subtitles", application_services.subtitle_jobs), ("gemini", application_services.gemini_subtitle_jobs), ("voice", application_services.voiceover_manager)):
        original = manager.shutdown

        def close(*, timeout_seconds, label=label, original=original):
            original(timeout_seconds=timeout_seconds)
            order.append(label)

        monkeypatch.setattr(manager, "shutdown", close)
    original_run_shutdown = application_services.run_manager.shutdown

    async def run_shutdown(timeout_seconds):
        await original_run_shutdown(timeout_seconds)
        order.append("crawler")

    monkeypatch.setattr(application_services.run_manager, "shutdown", run_shutdown)

    async def run():
        ready = asyncio.Event()

        async def scheduler():
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                order.append("scheduler")

        application_services.scheduler_task = asyncio.create_task(scheduler())
        await ready.wait()
        await application_services.shutdown()
        assert order[:2] == ["scheduler", "crawler"]
        assert set(order[2:-1]) == {"subtitles", "gemini", "voice"}
        assert order[-1] == "http"

    asyncio.run(run())


def test_crawler_shutdown_drains_store_thread_after_caller_is_cancelled(mongo_store):
    manager = RunManager({}, EventBus(), mongo_store)
    entered, release = threading.Event(), threading.Event()

    def write():
        entered.set()
        assert release.wait(3)

    async def run():
        pending = asyncio.create_task(manager._store_call(write))
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        stopping = asyncio.create_task(manager.shutdown(timeout_seconds=2))
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        await stopping
        assert not manager._storage_tasks
        with pytest.raises(RuntimeError, match="stopping"):
            await manager.start_batch(1)

    asyncio.run(run())
