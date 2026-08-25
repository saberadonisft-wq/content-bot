"""Visible, view-only browser wall for saved channels.

This boundary opens only application-owned profiles and stored channel URLs. It
does not inspect pages, extract DOM values, take screenshots, or refresh pages.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..crawlers import SOURCE_REGISTRY
from ..crawlers.runtime import (
    BrowserExecutableResolver,
    BrowserLaunchRequest,
    BrowserSession,
    CancellationToken,
    PlaywrightBrowserHandle,
    PlaywrightPersistentDriver,
    ProfileInUse,
    ProfileLock,
    ProfileNamespace,
)

logger = logging.getLogger("content_bot.live_wall")


class LiveWallFailure(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class LiveWallState(StrEnum):
    IDLE = "idle"
    OPENING = "opening"
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"
    CLOSING = "closing"


@dataclass(frozen=True, slots=True)
class DisplayBounds:
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class LiveWallChannel:
    id: str
    source_id: str
    label: str
    url: str


@dataclass(slots=True)
class LiveWallWindow:
    channel: LiveWallChannel
    target_id: str | None = None
    state: str = "opening"
    reason_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel.id,
            "source_id": self.channel.source_id,
            "label": self.channel.label,
            "state": self.state,
            "reason_code": self.reason_code,
        }


DriverFactory = Callable[[], PlaywrightPersistentDriver]


def tile_bounds(display: DisplayBounds, count: int, *, gap: int = 8) -> list[dict[str, int]]:
    if not 1 <= count <= 6:
        raise ValueError("Live Wall supports between one and six windows")
    if count == 1:
        columns, rows = 1, 1
    elif count == 2:
        columns, rows = 2, 1
    elif count <= 4:
        columns, rows = 2, 2
    else:
        columns, rows = 3, 2
    cell_width = max(1, (display.width - gap * (columns - 1)) // columns)
    cell_height = max(1, (display.height - gap * (rows - 1)) // rows)
    bounds: list[dict[str, int]] = []
    for index in range(count):
        column = index % columns
        row = index // columns
        bounds.append(
            {
                "left": display.left + column * (cell_width + gap),
                "top": display.top + row * (cell_height + gap),
                "width": cell_width,
                "height": cell_height,
            }
        )
    return bounds


class LiveWallManager:
    """Own the single physical Live Wall session on this machine."""

    def __init__(
        self,
        *,
        profile_root: Path,
        browser_executable: Path | None,
        driver_factory: DriverFactory = PlaywrightPersistentDriver,
    ) -> None:
        self.profile_root = profile_root.expanduser().resolve()
        self.browser_executable = browser_executable
        self.driver_factory = driver_factory
        self._lock = asyncio.Lock()
        self._owner_key: str | None = None
        self._keyword_id: int | None = None
        self._session_id: str | None = None
        self._session: BrowserSession | None = None
        self._handle: PlaywrightBrowserHandle | None = None
        self._cdp: Any = None
        self._cdp_anchor_page: Any = None
        self._anchor_target_id: str | None = None
        self._state = LiveWallState.IDLE
        self._reason_code: str | None = None
        self._detail = "No external Live Wall is open."
        self._windows: list[LiveWallWindow] = []

    async def open_or_update(
        self,
        owner_id: str,
        keyword_id: int,
        channels: list[LiveWallChannel],
        display: DisplayBounds,
    ) -> dict[str, Any]:
        if not 1 <= len(channels) <= 6:
            raise LiveWallFailure(
                "INVALID_CHANNEL_COUNT", "Choose between one and six saved channels."
            )
        owner_key = _owner_key(owner_id)
        async with self._lock:
            if self._owner_key is not None and self._owner_key != owner_key:
                raise LiveWallFailure(
                    "LIVE_WALL_IN_USE",
                    "Another Content Bot user is using the external Live Wall on this machine.",
                )
            self._state = LiveWallState.OPENING
            self._reason_code = None
            self._detail = "Opening the application-owned Cốc Cốc Live Wall."
            try:
                if self._session is None:
                    await self._open_browser(owner_id)
                await self._replace_windows(channels, display)
            except LiveWallFailure:
                if self._session is not None and self._owner_key is None:
                    await self._close_locked()
                self._state = LiveWallState.FAILED
                raise
            except Exception as exc:
                if self._session is not None and self._owner_key is None:
                    await self._close_locked()
                self._state = LiveWallState.FAILED
                self._reason_code, self._detail = _browser_failure(exc)
                logger.exception(
                    "External Live Wall failed with reason code %s",
                    self._reason_code,
                )
                raise LiveWallFailure(self._reason_code, self._detail) from exc
            self._owner_key = owner_key
            self._keyword_id = keyword_id
            self._session_id = self._session_id or uuid.uuid4().hex
            opened = sum(window.state == "open" for window in self._windows)
            if opened == 0:
                self._state = LiveWallState.FAILED
                self._reason_code = "WINDOW_OPEN_FAILED"
                self._detail = "Cốc Cốc could not create any Live Wall window."
            else:
                self._state = (
                    LiveWallState.READY
                    if opened == len(self._windows)
                    else LiveWallState.PARTIAL
                )
                self._detail = f"{opened}/{len(self._windows)} Live Wall windows are open."
            return await self._status_locked(owner_key)

    async def status(self, owner_id: str) -> dict[str, Any]:
        owner_key = _owner_key(owner_id)
        async with self._lock:
            return await self._status_locked(owner_key)

    async def close(self, owner_id: str) -> dict[str, Any]:
        owner_key = _owner_key(owner_id)
        async with self._lock:
            if self._owner_key is not None and self._owner_key != owner_key:
                raise LiveWallFailure(
                    "LIVE_WALL_IN_USE",
                    "Another Content Bot user owns the external Live Wall session.",
                )
            await self._close_locked()
            return await self._status_locked(owner_key)

    async def delete_profile(self, owner_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != "DELETE LIVE WALL PROFILE":
            raise LiveWallFailure(
                "PROFILE_CONFIRMATION_REQUIRED",
                "Profile deletion requires the exact confirmation: DELETE LIVE WALL PROFILE",
            )
        owner_key = _owner_key(owner_id)
        async with self._lock:
            if self._session is not None:
                raise LiveWallFailure(
                    "LIVE_WALL_PROFILE_IN_USE",
                    "Close the external Live Wall before deleting its login profile.",
                )
            profile = self._profile(owner_id)
            lock = ProfileLock(profile, owner_id=f"live-wall-delete-{owner_key}")
            lock.acquire()
            lock.release()
            if profile.path.exists():
                shutil.rmtree(profile.path)
            return {"deleted": True}

    async def shutdown(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _open_browser(self, owner_id: str) -> None:
        try:
            executable = BrowserExecutableResolver().resolve(self.browser_executable)
        except FileNotFoundError as exc:
            raise LiveWallFailure(
                "BROWSER_EXECUTABLE_NOT_FOUND",
                "Cốc Cốc was not found. Configure CONTENT_BOT_COCCOC_EXECUTABLE_PATH.",
            ) from exc
        session = BrowserSession(
            self.driver_factory(),
            BrowserLaunchRequest(executable, self._profile(owner_id)),
            owner_id=f"live-wall-{_owner_key(owner_id)}",
        )
        try:
            handle = await session.open(CancellationToken())
        except ImportError as exc:
            raise LiveWallFailure(
                "BROWSER_EXTRA_NOT_INSTALLED",
                "Install the optional Playwright browser runtime before using Live Wall.",
            ) from exc
        except Exception as exc:
            if "Playwright browser support is not installed" in str(exc):
                raise LiveWallFailure(
                    "BROWSER_EXTRA_NOT_INSTALLED",
                    "Install the optional Playwright browser runtime before using Live Wall.",
                ) from exc
            raise
        if not isinstance(handle, PlaywrightBrowserHandle):
            await session.close()
            raise LiveWallFailure(
                "BROWSER_DRIVER_UNSUPPORTED",
                "Live Wall requires the application-owned Playwright Chromium driver.",
            )
        try:
            browser = handle.context.browser
            if browser is not None and hasattr(browser, "new_browser_cdp_session"):
                cdp = await browser.new_browser_cdp_session()
                anchor_page = None
                anchor_target_id = None
            elif hasattr(handle.context, "new_cdp_session"):
                anchor_page = (
                    handle.context.pages[0]
                    if handle.context.pages
                    else await handle.context.new_page()
                )
                cdp = await handle.context.new_cdp_session(anchor_page)
                anchor_target_id = _target_id(await cdp.send("Target.getTargetInfo"))
            else:
                raise RuntimeError("CDP session creation is unavailable")
        except Exception as exc:
            await session.close()
            raise LiveWallFailure(
                "BROWSER_CDP_UNSUPPORTED",
                "This Cốc Cốc build does not support Live Wall window management.",
            ) from exc
        self._session = session
        self._handle = handle
        self._cdp = cdp
        self._cdp_anchor_page = anchor_page
        self._anchor_target_id = anchor_target_id

    async def _replace_windows(
        self, channels: list[LiveWallChannel], display: DisplayBounds
    ) -> None:
        if self._cdp is None or self._handle is None:
            raise LiveWallFailure("LIVE_WALL_NOT_OPEN", "The Live Wall browser is not open.")
        await self._close_targets()
        self._windows = [LiveWallWindow(channel) for channel in channels]
        geometries = tile_bounds(display, len(channels))
        for index, (window, bounds) in enumerate(
            zip(self._windows, geometries, strict=True)
        ):
            try:
                if index == 0 and self._cdp_anchor_page is not None:
                    await self._cdp_anchor_page.goto(
                        window.channel.url, wait_until="commit", timeout=15_000
                    )
                    target_id = self._anchor_target_id
                    if not target_id:
                        raise RuntimeError("The persistent browser target is unavailable")
                else:
                    result = await self._cdp.send(
                        "Target.createTarget",
                        {
                            "url": window.channel.url,
                            "newWindow": True,
                            **bounds,
                            "windowState": "normal",
                        },
                    )
                    target_id = _target_id(result)
                window.target_id = target_id
                window_info = await self._cdp.send(
                    "Browser.getWindowForTarget", {"targetId": target_id}
                )
                await self._cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_info["windowId"],
                        "bounds": {**bounds, "windowState": "normal"},
                    },
                )
                window.state = "open"
            except Exception:
                window.state = "failed"
                window.reason_code = "WINDOW_OPEN_FAILED"
        for page in tuple(self._handle.context.pages):
            if page is self._cdp_anchor_page:
                continue
            if page.url in {"about:blank", "chrome://newtab/"}:
                try:
                    await page.close()
                except Exception:
                    logger.debug("Could not close the initial Live Wall page", exc_info=True)

    async def _close_targets(self) -> None:
        if self._cdp is None:
            return
        for window in self._windows:
            if not window.target_id or window.target_id == self._anchor_target_id:
                continue
            try:
                await self._cdp.send("Target.closeTarget", {"targetId": window.target_id})
            except Exception:
                logger.debug("Could not close Live Wall target %s", window.target_id, exc_info=True)

    async def _refresh_window_states(self) -> None:
        if self._cdp is None or not self._windows:
            return
        try:
            payload = await self._cdp.send("Target.getTargets")
        except Exception:
            for window in self._windows:
                if window.state == "open":
                    window.state = "closed"
                    window.reason_code = "BROWSER_SESSION_CLOSED"
            return
        active = {
            str(info.get("targetId") or "")
            for info in payload.get("targetInfos", [])
            if info.get("type") in {"page", "tab"}
        }
        for window in self._windows:
            if window.target_id and window.target_id not in active and window.state == "open":
                window.state = "closed"
                window.reason_code = "WINDOW_CLOSED_BY_USER"

    async def _status_locked(self, owner_key: str) -> dict[str, Any]:
        owned = self._owner_key in {None, owner_key}
        if owned:
            await self._refresh_window_states()
        return {
            "session_id": self._session_id if owned else None,
            "keyword_id": self._keyword_id if owned else None,
            "state": self._state.value if self._owner_key is not None else "idle",
            "owned_by_current_user": owned,
            "reason_code": self._reason_code,
            "detail": (
                self._detail
                if owned
                else "Another Content Bot user owns the external Live Wall session."
            ),
            "windows": [window.as_dict() for window in self._windows] if owned else [],
        }

    async def _close_locked(self) -> None:
        if self._session is not None:
            self._state = LiveWallState.CLOSING
            try:
                await self._session.close()
            finally:
                self._session = None
                self._handle = None
                self._cdp = None
                self._cdp_anchor_page = None
                self._anchor_target_id = None
        self._owner_key = None
        self._keyword_id = None
        self._session_id = None
        self._windows = []
        self._state = LiveWallState.IDLE
        self._reason_code = None
        self._detail = "No external Live Wall is open."

    def _profile(self, owner_id: str):
        return ProfileNamespace(self.profile_root, SOURCE_REGISTRY).profile(
            "web", f"live-wall:{owner_id}"
        )


def _owner_key(owner_id: str) -> str:
    value = owner_id.strip()
    if not value or len(value) > 512:
        raise LiveWallFailure("INVALID_USER", "The authenticated user identifier is invalid.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _target_id(result: dict[str, Any]) -> str:
    direct = str(result.get("targetId") or "")
    if direct:
        return direct
    info = result.get("targetInfo")
    if isinstance(info, dict):
        value = str(info.get("targetId") or "")
        if value:
            return value
    infos = result.get("targetInfos") or []
    if infos and isinstance(infos[0], dict):
        value = str(infos[0].get("targetId") or "")
        if value:
            return value
    raise ValueError("Target.createTarget did not return a target ID")


def _browser_failure(exc: BaseException) -> tuple[str, str]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, ProfileInUse) for item in chain):
        return (
            "LIVE_WALL_PROFILE_IN_USE",
            "Profile Cốc Cốc Live Wall đang được một tiến trình Content Bot khác sử dụng.",
        )
    if any(isinstance(item, NotImplementedError) for item in chain):
        return (
            "BROWSER_SUBPROCESS_UNAVAILABLE",
            "Backend hiện tại không hỗ trợ khởi động tiến trình Cốc Cốc trên Windows.",
        )
    messages = " ".join(str(item).casefold() for item in chain)
    if "executable doesn't exist" in messages or "failed to launch" in messages:
        return (
            "BROWSER_LAUNCH_FAILED",
            "Không thể khởi động Cốc Cốc bằng profile Live Wall riêng.",
        )
    if "target page, context or browser has been closed" in messages:
        return (
            "BROWSER_CLOSED_DURING_STARTUP",
            "Cốc Cốc đã đóng trước khi Live Wall tạo xong các cửa sổ.",
        )
    return (
        "LIVE_WALL_BROWSER_FAILED",
        "Cốc Cốc không thể tạo các cửa sổ Live Wall đã yêu cầu.",
    )
