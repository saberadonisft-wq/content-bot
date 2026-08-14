from __future__ import annotations

import asyncio
import builtins
from pathlib import Path

import pytest

from app.crawlers import SOURCE_REGISTRY
from app.crawlers.runtime import (
    BrowserLaunchRequest,
    PlaywrightPersistentDriver,
    ProfileNamespace,
)
from app.crawlers.runtime.playwright_driver import (
    PlaywrightUnavailable,
    _start_playwright,
)


class FakeBrowser:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class FakeContext:
    def __init__(self) -> None:
        self.browser = FakeBrowser()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class FakeChromium:
    def __init__(self, context: FakeContext, *, fail: bool = False) -> None:
        self.context = context
        self.fail = fail
        self.kwargs = None

    async def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        if self.fail:
            raise RuntimeError("synthetic driver failure")
        return self.context


class FakeRuntime:
    def __init__(self, *, fail: bool = False) -> None:
        self.context = FakeContext()
        self.chromium = FakeChromium(self.context, fail=fail)
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


def request(tmp_path: Path) -> BrowserLaunchRequest:
    executable = tmp_path / "browser.exe"
    executable.touch()
    profile = ProfileNamespace(tmp_path / "profiles-v2", SOURCE_REGISTRY).profile(
        "bilibili"
    )
    return BrowserLaunchRequest(executable, profile)


def test_playwright_driver_uses_safe_visible_persistent_launch_contract(tmp_path: Path) -> None:
    async def run():
        runtime = FakeRuntime()

        async def factory():
            return runtime

        handle = await PlaywrightPersistentDriver(factory).launch_persistent(
            request(tmp_path)
        )
        await handle.close()
        return runtime, handle

    runtime, handle = asyncio.run(run())
    assert runtime.chromium.kwargs == {
        "user_data_dir": str(request(tmp_path).profile.path),
        "executable_path": str((tmp_path / "browser.exe").resolve()),
        "headless": False,
        "locale": "vi-VN",
        "accept_downloads": False,
        "chromium_sandbox": True,
    }
    assert "args" not in runtime.chromium.kwargs
    assert "proxy" not in runtime.chromium.kwargs
    assert runtime.context.close_calls == 1
    assert runtime.stop_calls == 1
    assert handle.closed is True


def test_playwright_driver_stops_runtime_when_launch_fails(tmp_path: Path) -> None:
    async def run():
        runtime = FakeRuntime(fail=True)

        async def factory():
            return runtime

        with pytest.raises(RuntimeError, match="synthetic"):
            await PlaywrightPersistentDriver(factory).launch_persistent(request(tmp_path))
        return runtime

    assert asyncio.run(run()).stop_calls == 1


def test_playwright_force_close_owns_browser_and_runtime(tmp_path: Path) -> None:
    async def run():
        runtime = FakeRuntime()

        async def factory():
            return runtime

        handle = await PlaywrightPersistentDriver(factory).launch_persistent(
            request(tmp_path)
        )
        await handle.force_close()
        await handle.force_close()
        return runtime

    runtime = asyncio.run(run())
    assert runtime.context.browser.close_calls == 1
    assert runtime.stop_calls == 1


def test_missing_playwright_dependency_is_typed(monkeypatch) -> None:
    original_import = builtins.__import__

    def missing_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("synthetic missing browser extra")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_playwright)
    with pytest.raises(PlaywrightUnavailable):
        asyncio.run(_start_playwright())
