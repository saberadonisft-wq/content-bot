"""Owned visible-browser boundary for clean-room platform adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from .contracts import CancellationToken
from .profiles import ProfileLock, ProfileRef


class BrowserSessionState(StrEnum):
    NEW = "new"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class BrowserExecutableNotFound(FileNotFoundError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserLaunchRequest:
    executable: Path
    profile: ProfileRef
    locale: str = "vi-VN"
    visible: bool = True

    def __post_init__(self) -> None:
        executable = self.executable.expanduser().resolve()
        if not executable.is_file():
            raise BrowserExecutableNotFound(str(executable))
        if not self.profile.owned:
            raise ValueError("CBCE may only launch an application-owned browser profile")
        if not self.visible:
            raise ValueError("Interactive browser sessions must remain visible")
        locale = self.locale.strip()
        if not locale or len(locale) > 35:
            raise ValueError("Browser locale is invalid")
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "locale", locale)


@runtime_checkable
class BrowserHandle(Protocol):
    @property
    def closed(self) -> bool: ...

    async def close(self) -> None: ...

    async def force_close(self) -> None: ...


@runtime_checkable
class BrowserDriver(Protocol):
    async def launch_persistent(self, request: BrowserLaunchRequest) -> BrowserHandle: ...


class BrowserExecutableResolver:
    """Resolve an explicitly configured browser from a bounded candidate list."""

    def __init__(self, candidates: Iterable[Path] = ()) -> None:
        self._candidates = tuple(Path(candidate) for candidate in candidates)

    def resolve(self, explicit: Path | None = None) -> Path:
        candidates = ((explicit,) if explicit is not None else ()) + self._candidates
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved.is_file():
                return resolved
        raise BrowserExecutableNotFound(
            "No configured browser executable exists; configure a Cốc Cốc or Chromium path."
        )


class BrowserSession:
    """Own one persistent context and its profile lock for the full session."""

    def __init__(
        self,
        driver: BrowserDriver,
        request: BrowserLaunchRequest,
        *,
        owner_id: str,
        close_timeout_seconds: float = 10,
        ownership_registry: object | None = None,
    ) -> None:
        if close_timeout_seconds <= 0:
            raise ValueError("Browser close timeout must be positive")
        self.driver = driver
        self.request = request
        self.close_timeout_seconds = close_timeout_seconds
        self.state = BrowserSessionState.NEW
        self._lock = ProfileLock(request.profile, owner_id=owner_id)
        self._handle: BrowserHandle | None = None
        self._ownership_registry = ownership_registry
        self._registered_ownership: object | None = None

    @property
    def handle(self) -> BrowserHandle:
        if self.state is not BrowserSessionState.OPEN or self._handle is None:
            raise RuntimeError("Browser session is not open")
        return self._handle

    async def open(self, cancellation: CancellationToken) -> BrowserHandle:
        if self.state is not BrowserSessionState.NEW:
            raise RuntimeError("Browser session can only be opened once")
        cancellation.raise_if_cancelled()
        self.state = BrowserSessionState.OPENING
        try:
            self._lock.acquire()
            cancellation.raise_if_cancelled()
            self._handle = await self.driver.launch_persistent(self.request)
            cancellation.raise_if_cancelled()
            if self._handle.closed:
                raise RuntimeError("Browser driver returned a closed handle")
            self._register_process_ownership(self._handle)
            self.state = BrowserSessionState.OPEN
            return self._handle
        except BaseException:
            self.state = BrowserSessionState.FAILED
            if self._handle is not None and not self._handle.closed:
                await self._ensure_closed(self._handle)
            self._unregister_process_ownership()
            self._lock.release()
            raise

    async def close(self) -> None:
        if self.state is BrowserSessionState.CLOSED:
            return
        if self.state is BrowserSessionState.NEW:
            self.state = BrowserSessionState.CLOSED
            return
        self.state = BrowserSessionState.CLOSING
        handle = self._handle
        if handle is not None and not handle.closed:
            await self._ensure_closed(handle)
        self._unregister_process_ownership()
        self._lock.release()
        self.state = BrowserSessionState.CLOSED

    def _register_process_ownership(self, handle: BrowserHandle) -> None:
        ownership = getattr(handle, "process_ownership", None)
        if ownership is None:
            return
        if self._ownership_registry is None:
            # Import lazily so the provider-neutral crawler runtime does not
            # require application services for sessions that expose no PID.
            from app.services.system_hygiene import BROWSER_PROCESS_OWNERSHIP

            self._ownership_registry = BROWSER_PROCESS_OWNERSHIP
        register = getattr(self._ownership_registry, "register", None)
        if not callable(register):
            raise TypeError("ownership_registry must expose register()")
        register(ownership)
        self._registered_ownership = ownership

    def _unregister_process_ownership(self) -> None:
        if self._registered_ownership is None or self._ownership_registry is None:
            return
        unregister = getattr(self._ownership_registry, "unregister", None)
        if callable(unregister):
            unregister(self._registered_ownership)
        self._registered_ownership = None

    async def _ensure_closed(self, handle: BrowserHandle) -> None:
        try:
            await asyncio.wait_for(handle.close(), timeout=self.close_timeout_seconds)
        except TimeoutError:
            await asyncio.wait_for(
                handle.force_close(), timeout=self.close_timeout_seconds
            )
        if not handle.closed:
            raise RuntimeError("Browser driver did not close its owned browser")

    async def __aenter__(self) -> BrowserHandle:
        return await self.open(CancellationToken())

    async def __aexit__(self, *_args) -> None:
        await self.close()
