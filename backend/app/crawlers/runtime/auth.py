"""Interactive authentication state machine without challenge automation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from .contracts import CancellationToken
from .errors import CrawlerErrorCode, CrawlerFailure


class AuthState(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    CHECKING_SESSION = "checking_session"
    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    CHALLENGE_REQUIRED = "challenge_required"
    WAITING_FOR_USER = "waiting_for_user"
    RUNNING = "running"
    CLOSING = "closing"


class SessionStatus(StrEnum):
    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    CHALLENGE_REQUIRED = "challenge_required"


@dataclass(frozen=True, slots=True)
class SessionProbe:
    status: SessionStatus
    safe_message: str = ""


@dataclass(frozen=True, slots=True)
class AuthTransition:
    previous: AuthState
    current: AuthState
    reason: str


ProbeSession = Callable[[], Awaitable[SessionProbe]]
TransitionHandler = Callable[[AuthTransition], Awaitable[None]]


_ALLOWED: dict[AuthState, set[AuthState]] = {
    AuthState.CLOSED: {AuthState.OPENING},
    AuthState.OPENING: {AuthState.CHECKING_SESSION, AuthState.CLOSING},
    AuthState.CHECKING_SESSION: {
        AuthState.READY,
        AuthState.AUTH_REQUIRED,
        AuthState.CHALLENGE_REQUIRED,
        AuthState.CLOSING,
    },
    AuthState.AUTH_REQUIRED: {AuthState.WAITING_FOR_USER, AuthState.CLOSING},
    AuthState.CHALLENGE_REQUIRED: {AuthState.WAITING_FOR_USER, AuthState.CLOSING},
    AuthState.WAITING_FOR_USER: {AuthState.CHECKING_SESSION, AuthState.CLOSING},
    AuthState.READY: {AuthState.RUNNING, AuthState.CHECKING_SESSION, AuthState.CLOSING},
    AuthState.RUNNING: {AuthState.READY, AuthState.CLOSING},
    AuthState.CLOSING: {AuthState.CLOSED},
}


class AuthStateMachine:
    def __init__(self, *, on_transition: TransitionHandler | None = None) -> None:
        self.state = AuthState.CLOSED
        self.history: list[AuthTransition] = []
        self._on_transition = on_transition
        self._continue = asyncio.Event()

    async def establish(
        self,
        probe_session: ProbeSession,
        *,
        interactive: bool,
        timeout_seconds: float,
        cancellation: CancellationToken,
        clock: Callable[[], float] = time.monotonic,
    ) -> SessionProbe:
        if timeout_seconds <= 0:
            raise ValueError("Auth timeout must be positive")
        await self._transition(AuthState.OPENING, "browser_opening")
        deadline = clock() + timeout_seconds
        while True:
            cancellation.raise_if_cancelled()
            await self._transition(AuthState.CHECKING_SESSION, "session_probe")
            probe = await probe_session()
            if probe.status is SessionStatus.READY:
                await self._transition(AuthState.READY, probe.safe_message or "session_ready")
                return probe

            required_state = (
                AuthState.AUTH_REQUIRED
                if probe.status is SessionStatus.AUTH_REQUIRED
                else AuthState.CHALLENGE_REQUIRED
            )
            await self._transition(required_state, probe.safe_message or probe.status.value)
            if not interactive:
                code = (
                    CrawlerErrorCode.AUTH_REQUIRED
                    if probe.status is SessionStatus.AUTH_REQUIRED
                    else CrawlerErrorCode.CHALLENGE_REQUIRED
                )
                raise CrawlerFailure(
                    code,
                    probe.safe_message
                    or "Interactive authentication is required; background run was not opened.",
                )
            await self._transition(AuthState.WAITING_FOR_USER, probe.status.value)
            remaining = deadline - clock()
            if remaining <= 0:
                raise self._timeout()
            continue_task = asyncio.create_task(self._continue.wait())
            cancel_task = asyncio.create_task(cancellation.wait())
            try:
                done, pending = await asyncio.wait(
                    {continue_task, cancel_task},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if not done:
                    raise self._timeout()
                cancellation.raise_if_cancelled()
                self._continue.clear()
            finally:
                for task in (continue_task, cancel_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(continue_task, cancel_task, return_exceptions=True)

    def continue_after_user_action(self) -> None:
        if self.state is not AuthState.WAITING_FOR_USER:
            raise RuntimeError("Auth continuation is only valid while waiting for the user")
        self._continue.set()

    async def mark_running(self) -> None:
        await self._transition(AuthState.RUNNING, "crawl_started")

    async def mark_ready(self) -> None:
        await self._transition(AuthState.READY, "crawl_finished")

    async def close(self) -> None:
        if self.state is AuthState.CLOSED:
            return
        if self.state is not AuthState.CLOSING:
            await self._transition(AuthState.CLOSING, "closing")
        await self._transition(AuthState.CLOSED, "closed")

    async def _transition(self, next_state: AuthState, reason: str) -> None:
        if next_state not in _ALLOWED.get(self.state, set()):
            raise RuntimeError(f"Invalid auth transition: {self.state} -> {next_state}")
        transition = AuthTransition(self.state, next_state, reason[:300])
        self.state = next_state
        self.history.append(transition)
        if self._on_transition:
            await self._on_transition(transition)

    @staticmethod
    def _timeout() -> CrawlerFailure:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_TIMEOUT,
            "Timed out while waiting for interactive authentication.",
            retryable=True,
        )
