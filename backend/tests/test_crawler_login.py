from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.crawler_profiles import build_crawler_profiles_router
from app.config import settings
from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.crawler_login import CrawlerLoginManager, LoginSessionState


def _configure(monkeypatch, tmp_path: Path) -> Path:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles-v2"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    monkeypatch.setattr(
        "app.services.crawler_login.cbce_browser_preflight",
        lambda: {"ready": True, "reason_code": None},
    )
    return executable


def test_login_manager_runs_one_session_and_cancel_closes_it(
    monkeypatch, tmp_path: Path
) -> None:
    executable = _configure(monkeypatch, tmp_path)
    opened = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake_login(
        source_id: str,
        *,
        executable: Path,
        profile_root: Path,
        timeout_seconds: float,
    ) -> None:
        calls.append((source_id, executable, profile_root, timeout_seconds))
        opened.set()
        await release.wait()

    monkeypatch.setattr(
        "app.services.crawler_login.open_manual_login", fake_login
    )

    async def run():
        manager = CrawlerLoginManager()
        first = await manager.start("xhs", timeout_seconds=60)
        await opened.wait()
        repeated = await manager.start("xhs", timeout_seconds=60)
        stopped = await manager.stop("xhs")
        return first, repeated, stopped

    first, repeated, stopped = asyncio.run(run())

    assert first.state is LoginSessionState.WAITING_FOR_USER
    assert repeated.started_at == first.started_at
    assert stopped.state is LoginSessionState.CANCELLED
    assert calls == [("xhs", executable.resolve(), tmp_path / "profiles-v2", 60)]


def test_login_manager_completion_and_failure_are_redacted(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    async def completed(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "app.services.crawler_login.open_manual_login", completed
    )

    async def probe(*_args, **_kwargs):
        return {
            "schema_version": "cbce.dom-structure.v1",
            "source_id": "weibo",
            "element_count": 12,
        }

    monkeypatch.setattr("app.services.crawler_login.probe_dom", probe)

    async def complete_run():
        manager = CrawlerLoginManager()
        await manager.start("weibo", timeout_seconds=60)
        await manager._tasks["weibo"]
        return manager.status("weibo")

    completed_status = asyncio.run(complete_run())
    assert completed_status.state is LoginSessionState.COMPLETED
    assert completed_status.observation_ready is True
    assert len(completed_status.observation_digest or "") == 64
    observation = tmp_path / "data/cbce-dom-observations/weibo-latest.json"
    assert observation.is_file()
    assert str(tmp_path) not in observation.read_text(encoding="utf-8")

    async def failed(*_args, **_kwargs) -> None:
        raise RuntimeError("private-profile-path and browser diagnostic")

    monkeypatch.setattr("app.services.crawler_login.open_manual_login", failed)

    async def failed_run():
        manager = CrawlerLoginManager()
        await manager.start("zhihu", timeout_seconds=60)
        await asyncio.sleep(0)
        return manager.status("zhihu")

    failure = asyncio.run(failed_run())
    assert failure.state is LoginSessionState.FAILED
    assert failure.reason_code == "LOGIN_SESSION_FAILED"
    assert "private-profile" not in failure.detail


def test_login_manager_reports_typed_value_free_probe_gate(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    async def completed(*_args, **_kwargs) -> None:
        return None

    async def auth_required(*_args, **_kwargs):
        raise CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "Profile is still logged out.",
        )

    monkeypatch.setattr("app.services.crawler_login.open_manual_login", completed)
    monkeypatch.setattr("app.services.crawler_login.probe_dom", auth_required)

    async def run():
        manager = CrawlerLoginManager()
        await manager.start("xhs", timeout_seconds=60)
        await manager._tasks["xhs"]
        return manager.status("xhs")

    status = asyncio.run(run())
    assert status.state is LoginSessionState.FAILED
    assert status.reason_code == "AUTH_REQUIRED"
    assert status.observation_ready is False


def test_login_api_fails_closed_on_preflight_and_never_returns_profile_paths(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.services.crawler_login.cbce_browser_preflight",
        lambda: {
            "ready": False,
            "reason_code": "BROWSER_EXECUTABLE_OR_PROFILE_INVALID",
        },
    )
    app = FastAPI()
    app.include_router(build_crawler_profiles_router(CrawlerLoginManager()))
    client = TestClient(app)

    response = client.post(
        "/api/v1/crawler/profiles/xhs/login",
        json={"timeout_seconds": 60},
    )
    assert response.status_code == 409
    assert str(tmp_path) not in response.text
    assert client.get("/api/v1/crawler/profiles/x/login").status_code == 404


def test_login_api_starts_and_stops_visible_session(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    release = asyncio.Event()

    async def fake_login(*_args, **_kwargs) -> None:
        await release.wait()

    monkeypatch.setattr(
        "app.services.crawler_login.open_manual_login", fake_login
    )
    manager = CrawlerLoginManager()
    app = FastAPI()
    app.include_router(build_crawler_profiles_router(manager))
    with TestClient(app) as client:
        started = client.post(
            "/api/v1/crawler/profiles/douyin/login",
            json={"timeout_seconds": 60},
        )
        assert started.status_code == 202
        assert started.json()["state"] == "waiting_for_user"
        status = client.get("/api/v1/crawler/profiles/douyin/login")
        assert status.json()["state"] == "waiting_for_user"
        stopped = client.delete("/api/v1/crawler/profiles/douyin/login")
        assert stopped.json()["state"] == "cancelled"
