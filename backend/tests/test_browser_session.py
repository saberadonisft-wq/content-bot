from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.crawlers import SOURCE_REGISTRY
from app.crawlers.runtime import (
    BrowserExecutableNotFound,
    BrowserExecutableResolver,
    BrowserLaunchRequest,
    BrowserSession,
    BrowserSessionState,
    CancellationToken,
    ProfileInUse,
    ProfileNamespace,
)
from app.services.system_hygiene import (
    BrowserProcessOwnership,
    BrowserProcessOwnershipRegistry,
)


class FakeHandle:
    def __init__(self, *, close_hangs: bool = False) -> None:
        self._closed = False
        self.close_hangs = close_hangs
        self.close_calls = 0
        self.force_close_calls = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_hangs:
            await asyncio.Event().wait()
        self._closed = True

    async def force_close(self) -> None:
        self.force_close_calls += 1
        self._closed = True


class OwnedFakeHandle(FakeHandle):
    def __init__(self) -> None:
        super().__init__()
        self.process_ownership = BrowserProcessOwnership(
            pid=987, creation_time="fixture-created", parent_pid=12
        )

class FakeDriver:
    def __init__(self, handle: FakeHandle | None = None, *, fail: bool = False) -> None:
        self.handle = handle or FakeHandle()
        self.fail = fail
        self.requests: list[BrowserLaunchRequest] = []

    async def launch_persistent(self, request: BrowserLaunchRequest) -> FakeHandle:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("synthetic launch failure")
        return self.handle


def launch_request(tmp_path: Path):
    executable = tmp_path / "browser.exe"
    executable.touch()
    profile = ProfileNamespace(
        tmp_path / "profiles-v2", SOURCE_REGISTRY
    ).profile("bilibili", "test-account")
    return BrowserLaunchRequest(executable=executable, profile=profile)


def test_browser_session_owns_visible_persistent_profile_for_lifetime(tmp_path: Path) -> None:
    async def run():
        request = launch_request(tmp_path)
        driver = FakeDriver()
        session = BrowserSession(driver, request, owner_id="run-1")
        handle = await session.open(CancellationToken())
        assert session.state is BrowserSessionState.OPEN
        assert session.handle is handle
        competing = BrowserSession(FakeDriver(), request, owner_id="run-2")
        with pytest.raises(ProfileInUse):
            await competing.open(CancellationToken())
        await session.close()
        await session.close()
        replacement = BrowserSession(FakeDriver(), request, owner_id="run-3")
        await replacement.open(CancellationToken())
        await replacement.close()
        return driver, handle, session

    driver, handle, session = asyncio.run(run())
    assert driver.requests[0].visible is True
    assert driver.requests[0].profile.owned is True
    assert handle.closed is True
    assert session.state is BrowserSessionState.CLOSED


def test_browser_launch_failure_releases_profile_lock(tmp_path: Path) -> None:
    async def run():
        request = launch_request(tmp_path)
        failed = BrowserSession(FakeDriver(fail=True), request, owner_id="run-fail")
        with pytest.raises(RuntimeError, match="synthetic"):
            await failed.open(CancellationToken())
        replacement = BrowserSession(FakeDriver(), request, owner_id="run-next")
        await replacement.open(CancellationToken())
        await replacement.close()
        return failed

    assert asyncio.run(run()).state is BrowserSessionState.FAILED


def test_browser_close_timeout_forces_owned_handle_closed(tmp_path: Path) -> None:
    async def run():
        handle = FakeHandle(close_hangs=True)
        session = BrowserSession(
            FakeDriver(handle),
            launch_request(tmp_path),
            owner_id="run-timeout",
            close_timeout_seconds=0.01,
        )
        await session.open(CancellationToken())
        await session.close()
        return handle

    handle = asyncio.run(run())
    assert handle.close_calls == 1
    assert handle.force_close_calls == 1
    assert handle.closed is True


def test_browser_session_registers_and_releases_driver_process_identity(tmp_path: Path) -> None:
    async def run():
        registry = BrowserProcessOwnershipRegistry()
        handle = OwnedFakeHandle()
        session = BrowserSession(
            FakeDriver(handle), launch_request(tmp_path), owner_id="run-owned",
            ownership_registry=registry,
        )
        await session.open(CancellationToken())
        during = registry.snapshot()
        await session.close()
        return during, registry.snapshot()

    during, after = asyncio.run(run())
    assert during == (BrowserProcessOwnership(987, "fixture-created", 12),)
    assert after == ()


def test_browser_session_rejects_external_profile_and_cancelled_open(tmp_path: Path) -> None:
    request = launch_request(tmp_path)
    with pytest.raises(ValueError, match="application-owned"):
        BrowserLaunchRequest(
            executable=request.executable,
            profile=type(request.profile)(
                request.profile.source_id,
                request.profile.account_key,
                request.profile.path,
                owned=False,
            ),
        )

    token = CancellationToken()
    token.cancel()

    async def run():
        session = BrowserSession(FakeDriver(), request, owner_id="cancelled")
        with pytest.raises(Exception) as captured:
            await session.open(token)
        return captured.value, session

    _failure, session = asyncio.run(run())
    assert session.state is BrowserSessionState.NEW


def test_browser_executable_resolver_is_bounded_and_requires_real_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.exe"
    found = tmp_path / "CocCoc.exe"
    found.touch()
    resolver = BrowserExecutableResolver((missing, found))
    assert resolver.resolve() == found.resolve()
    with pytest.raises(BrowserExecutableNotFound):
        BrowserExecutableResolver((missing,)).resolve()
