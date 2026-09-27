"""Optional Playwright driver for an owned persistent Chromium context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .browser import BrowserHandle, BrowserLaunchRequest


class PlaywrightUnavailable(RuntimeError):
    pass


class PlaywrightBrowserHandle(BrowserHandle):
    def __init__(self, runtime: Any, context: Any) -> None:
        self.runtime = runtime
        self.context = context
        self._closed = False
        # Playwright's public BrowserContext API does not expose the spawned
        # Chromium PID. Drivers that can obtain a verified identity may set
        # this optional value before BrowserSession registers the handle.
        self.process_ownership = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self.context.close()
        finally:
            await self.runtime.stop()
            self._closed = True

    async def force_close(self) -> None:
        if self._closed:
            return
        try:
            browser = getattr(self.context, "browser", None)
            if browser is not None:
                await browser.close()
            else:
                await self.context.close()
        finally:
            await self.runtime.stop()
            self._closed = True


RuntimeFactory = Callable[[], Awaitable[Any]]


class PlaywrightPersistentDriver:
    """Launch a visible sandboxed context; never attach to a personal browser."""

    def __init__(self, runtime_factory: RuntimeFactory | None = None) -> None:
        self._runtime_factory = runtime_factory or _start_playwright

    async def launch_persistent(
        self, request: BrowserLaunchRequest
    ) -> PlaywrightBrowserHandle:
        runtime = await self._runtime_factory()
        try:
            context = await runtime.chromium.launch_persistent_context(
                user_data_dir=str(request.profile.path),
                executable_path=str(request.executable),
                headless=False,
                locale=request.locale,
                accept_downloads=False,
                chromium_sandbox=True,
            )
        except BaseException:
            await runtime.stop()
            raise
        return PlaywrightBrowserHandle(runtime, context)


async def _start_playwright() -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PlaywrightUnavailable(
            "Playwright browser support is not installed; install the backend browser extra."
        ) from exc
    return await async_playwright().start()
