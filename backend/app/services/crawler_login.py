"""Supervise visible manual login sessions for application-owned CBCE profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..config import settings
from ..crawlers.dom_probe import DOM_PROBE_SPECS, probe_dom
from ..crawlers.login_session import LOGIN_HOMEPAGES, open_manual_login
from ..crawlers.runtime import (
    BrowserExecutableResolver,
    CrawlerFailure,
    normalize_account_ref,
)
from .cbce_runtime import cbce_browser_preflight


class LoginSessionState(StrEnum):
    IDLE = "idle"
    OPENING = "opening"
    WAITING_FOR_USER = "waiting_for_user"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(slots=True)
class LoginSessionStatus:
    source_id: str
    state: LoginSessionState = LoginSessionState.IDLE
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reason_code: str | None = None
    detail: str = "No manual login session is running."
    observation_ready: bool = False
    observation_digest: str | None = None
    connection_id: str = "default"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "connection_id": self.connection_id,
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "observation_ready": self.observation_ready,
            "observation_digest": self.observation_digest,
        }


class CrawlerLoginManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._statuses: dict[str, LoginSessionStatus] = {}

    async def start(
        self,
        source_id: str,
        *,
        timeout_seconds: float = 1_200,
        connection_id: str = "default",
    ) -> LoginSessionStatus:
        source = self._source(source_id)
        connection = self._connection(connection_id)
        session_key = self._session_key(source, connection)
        if not 30 <= timeout_seconds <= 7_200:
            raise ValueError("Login timeout must be between 30 and 7200 seconds")
        preflight = cbce_browser_preflight()
        if not preflight["ready"]:
            raise RuntimeError(str(preflight["reason_code"] or "CBCE_PREFLIGHT_FAILED"))
        executable = BrowserExecutableResolver().resolve(
            settings.content_bot_cbce_browser_executable_path
            or settings.content_bot_coccoc_executable_path
        )
        async with self._lock:
            existing = self._tasks.get(session_key)
            if existing is not None and not existing.done():
                return self._status(source, connection)
            status = LoginSessionStatus(
                source_id=source,
                connection_id=connection,
                state=LoginSessionState.OPENING,
                started_at=datetime.now(UTC),
                detail="Opening the visible application-owned browser profile.",
            )
            self._statuses[session_key] = status
            task = asyncio.create_task(
                self._run(
                    source,
                    connection,
                    session_key,
                    executable=executable,
                    timeout_seconds=timeout_seconds,
                ),
                name=f"cbce-manual-login-{session_key}",
            )
            self._tasks[session_key] = task
            await asyncio.sleep(0)
            return self._status(source, connection)

    async def stop(
        self, source_id: str, *, connection_id: str = "default"
    ) -> LoginSessionStatus:
        source = self._source(source_id)
        connection = self._connection(connection_id)
        session_key = self._session_key(source, connection)
        async with self._lock:
            task = self._tasks.get(session_key)
            if task is None or task.done():
                return self._status(source, connection)
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return self._status(source, connection)

    def status(
        self, source_id: str, *, connection_id: str = "default"
    ) -> LoginSessionStatus:
        source = self._source(source_id)
        return self._status(source, self._connection(connection_id))

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = tuple(task for task in self._tasks.values() if not task.done())
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        source_id: str,
        connection_id: str,
        session_key: str,
        *,
        executable,
        timeout_seconds: float,
    ) -> None:
        status = self._statuses[session_key]
        status.state = LoginSessionState.WAITING_FOR_USER
        status.detail = (
            "Complete login or challenge in the visible browser, then close its tab."
        )
        try:
            login_kwargs = {
                "executable": executable,
                "profile_root": settings.content_bot_cbce_profile_root,
                "timeout_seconds": timeout_seconds,
            }
            if connection_id != "default":
                login_kwargs["account_ref"] = connection_id
            await open_manual_login(source_id, **login_kwargs)
            if source_id in DOM_PROBE_SPECS:
                status.state = LoginSessionState.VERIFYING
                status.detail = (
                    "Login tab closed; verifying the retained profile with a value-free DOM probe."
                )
                probe_kwargs = {
                    "executable": executable,
                    "profile_root": settings.content_bot_cbce_profile_root,
                    "auth_timeout_seconds": min(timeout_seconds, 120),
                }
                if connection_id != "default":
                    probe_kwargs["account_ref"] = connection_id
                observation = await probe_dom(source_id, **probe_kwargs)
                digest = await asyncio.to_thread(
                    _store_observation,
                    source_id,
                    observation,
                    connection_id,
                )
                status.observation_ready = True
                status.observation_digest = digest
        except asyncio.CancelledError:
            status.state = LoginSessionState.CANCELLED
            status.reason_code = "LOGIN_CANCELLED"
            status.detail = "Manual login session was cancelled and the browser was closed."
            raise
        except TimeoutError:
            status.state = LoginSessionState.FAILED
            status.reason_code = "AUTH_TIMEOUT"
            status.detail = "Manual login timed out and the browser was closed."
        except CrawlerFailure as exc:
            status.state = LoginSessionState.FAILED
            status.reason_code = exc.code.value
            status.detail = exc.safe_message
        except Exception:
            status.state = LoginSessionState.FAILED
            status.reason_code = "LOGIN_SESSION_FAILED"
            status.detail = "Manual login session failed and owned resources were closed."
        else:
            status.state = LoginSessionState.COMPLETED
            status.detail = (
                "Profile retained and value-free DOM observation saved for review."
                if status.observation_ready
                else "Browser tab closed; the application-owned profile was retained."
            )
        finally:
            status.finished_at = datetime.now(UTC)

    def _status(self, source_id: str, connection_id: str) -> LoginSessionStatus:
        session_key = self._session_key(source_id, connection_id)
        status = self._statuses.get(session_key)
        if status is None:
            return LoginSessionStatus(source_id, connection_id=connection_id)
        return LoginSessionStatus(
            source_id=status.source_id,
            connection_id=status.connection_id,
            state=status.state,
            started_at=status.started_at,
            finished_at=status.finished_at,
            reason_code=status.reason_code,
            detail=status.detail,
            observation_ready=status.observation_ready,
            observation_digest=status.observation_digest,
        )

    @staticmethod
    def _connection(connection_id: str | None) -> str:
        if connection_id is None:
            return "default"
        try:
            normalized = normalize_account_ref(connection_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("connection_id không hợp lệ") from exc
        if len(normalized) > 128:
            raise ValueError("connection_id tối đa 128 ký tự")
        return normalized

    @staticmethod
    def _session_key(source_id: str, connection_id: str) -> str:
        if connection_id == "default":
            # Keep the original internal key for backwards compatibility with
            # callers/tests that inspect the default session task.
            return source_id
        digest = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:24]
        return f"{source_id}:{digest}"

    @staticmethod
    def _source(source_id: str) -> str:
        source = str(source_id).strip().casefold()
        if source not in LOGIN_HOMEPAGES:
            raise ValueError("Source does not use a CBCE browser login profile")
        return source


def _store_observation(
    source_id: str,
    observation: dict[str, object],
    connection_id: str = "default",
) -> str:
    serialized = json.dumps(
        observation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    root = (settings.data_dir / "cbce-dom-observations").resolve()
    root.mkdir(parents=True, exist_ok=True)
    suffix = (
        "latest"
        if connection_id == "default"
        else f"{hashlib.sha256(connection_id.encode('utf-8')).hexdigest()[:24]}-latest"
    )
    destination = root / f"{source_id}-{suffix}.json"
    temporary = root / f".{source_id}-{suffix}.json.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest
