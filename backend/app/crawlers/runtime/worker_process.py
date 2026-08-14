"""Owned subprocess execution for the versioned CBCE worker protocol."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .protocol import (
    MAX_MESSAGE_BYTES,
    SequenceTracker,
    WorkerEnvelope,
    WorkerMessageKind,
    decode_message,
    encode_message,
    safe_diagnostic,
)
from .redaction import contains_secret, is_secret_key


class WorkerProtocolError(RuntimeError):
    pass


class WorkerExited(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None, stderr_tail: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


@dataclass(frozen=True, slots=True)
class WorkerProcessSpec:
    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    ready_timeout_seconds: float = 10
    heartbeat_timeout_seconds: float = 30
    graceful_shutdown_seconds: float = 5
    hard_kill_seconds: float = 5
    stderr_tail_bytes: int = 16_384

    def __post_init__(self) -> None:
        if not self.command or not self.command[0]:
            raise ValueError("Worker command cannot be empty")
        if any(value <= 0 for value in (
            self.ready_timeout_seconds,
            self.heartbeat_timeout_seconds,
            self.graceful_shutdown_seconds,
            self.hard_kill_seconds,
            self.stderr_tail_bytes,
        )):
            raise ValueError("Worker timeouts and stderr tail size must be positive")
        awaiting_secret_value = False
        for argument in self.command:
            normalized = argument.lstrip("-/").split("=", 1)[0]
            if awaiting_secret_value or is_secret_key(normalized) or contains_secret(argument):
                raise ValueError("Secrets must not be passed in worker argv")
            awaiting_secret_value = argument.startswith(("-", "/")) and is_secret_key(
                normalized
            )
        for key, value in self.environment.items():
            if is_secret_key(key) or contains_secret(value):
                raise ValueError("Secrets must not be passed in worker environment")


@dataclass(frozen=True, slots=True)
class WorkerProcessResult:
    terminal_kind: WorkerMessageKind
    returncode: int
    message_count: int
    stderr_tail: str


MessageHandler = Callable[[WorkerEnvelope], Awaitable[None]]


class WorkerControl:
    def __init__(self, maxsize: int = 10) -> None:
        self._queue: asyncio.Queue[WorkerMessageKind] = asyncio.Queue(maxsize=maxsize)

    async def cancel(self) -> None:
        await self._queue.put(WorkerMessageKind.CANCEL)

    async def auth_continue(self) -> None:
        await self._queue.put(WorkerMessageKind.AUTH_CONTINUE)

    async def shutdown(self) -> None:
        await self._queue.put(WorkerMessageKind.SHUTDOWN)

    async def next(self) -> WorkerMessageKind:
        return await self._queue.get()


class WorkerProcessSupervisor:
    async def execute(
        self,
        spec: WorkerProcessSpec,
        start_message: WorkerEnvelope,
        *,
        on_message: MessageHandler,
        control: WorkerControl | None = None,
    ) -> WorkerProcessResult:
        if start_message.kind is not WorkerMessageKind.START:
            raise ValueError("Worker execution requires a start message")
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            *spec.command,
            cwd=spec.cwd,
            env=dict(spec.environment),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=start_new_session,
            limit=MAX_MESSAGE_BYTES + 1,
        )
        windows_job = _WindowsJob.attach(process.pid) if os.name == "nt" else None
        assert process.stdin and process.stdout and process.stderr
        stderr_tail = bytearray()
        stderr_task = asyncio.create_task(
            self._drain_stderr(process.stderr, stderr_tail, spec.stderr_tail_bytes)
        )
        message_count = 0
        terminal: WorkerMessageKind | None = None
        tracker = SequenceTracker()
        control_sequence = start_message.sequence
        try:
            ready = await self._read_message(
                process.stdout, timeout=spec.ready_timeout_seconds
            )
            if ready.kind is not WorkerMessageKind.READY:
                raise WorkerProtocolError("Worker did not emit ready as its first message")
            await self._send(process, start_message)

            while terminal is None:
                read_task = asyncio.create_task(process.stdout.readline())
                control_task = (
                    asyncio.create_task(control.next()) if control is not None else None
                )
                waiting = {read_task}
                if control_task is not None:
                    waiting.add(control_task)
                done, pending = await asyncio.wait(
                    waiting,
                    timeout=spec.heartbeat_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    for task in pending:
                        task.cancel()
                    raise WorkerProtocolError("Worker heartbeat timed out")
                if control_task is not None and control_task in done:
                    kind = control_task.result()
                    control_sequence += 1
                    await self._send(
                        process,
                        WorkerEnvelope(
                            kind=kind,
                            sequence=control_sequence,
                            run_id=start_message.run_id,
                            source_run_id=start_message.source_run_id,
                            source_id=start_message.source_id,
                            provider_id=start_message.provider_id,
                            operation=start_message.operation,
                        ),
                    )
                    if read_task not in done:
                        read_task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        continue
                if control_task is not None and control_task not in done:
                    control_task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                line = read_task.result()
                if not line:
                    break
                message = decode_message(line)
                if message.run_id != start_message.run_id:
                    raise WorkerProtocolError("Worker emitted an event for the wrong run")
                if not tracker.accept(message):
                    continue
                message_count += 1
                await on_message(message)
                if message.kind in {
                    WorkerMessageKind.COMPLETE,
                    WorkerMessageKind.ERROR,
                    WorkerMessageKind.CANCELLED,
                }:
                    terminal = message.kind

            if terminal is None:
                raise WorkerProtocolError("Worker exited without a terminal message")
            await self._wait_or_terminate(process, spec, windows_job)
            await stderr_task
            tail = stderr_tail.decode("utf-8", errors="replace")
            if terminal is WorkerMessageKind.ERROR or process.returncode != 0:
                raise WorkerExited(
                    "Crawler worker reported a failure.",
                    returncode=process.returncode,
                    stderr_tail=tail,
                )
            return WorkerProcessResult(
                terminal_kind=terminal,
                returncode=int(process.returncode or 0),
                message_count=message_count,
                stderr_tail=tail,
            )
        except asyncio.CancelledError:
            await self._soft_cancel(process, start_message, control_sequence + 1)
            await self._wait_or_terminate(process, spec, windows_job)
            raise
        finally:
            if process.returncode is None:
                await self._terminate_tree(process, spec, windows_job)
            if windows_job is not None:
                windows_job.close()
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    @staticmethod
    async def _read_message(
        reader: asyncio.StreamReader, *, timeout: float
    ) -> WorkerEnvelope:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except (TimeoutError, ValueError) as exc:
            raise WorkerProtocolError("Worker ready message timed out or exceeded size") from exc
        if not line:
            raise WorkerProtocolError("Worker exited before ready")
        return decode_message(line)

    @staticmethod
    async def _send(
        process: asyncio.subprocess.Process, message: WorkerEnvelope
    ) -> None:
        if process.stdin is None or process.stdin.is_closing():
            raise WorkerProtocolError("Worker stdin is closed")
        process.stdin.write(encode_message(message))
        await process.stdin.drain()

    async def _soft_cancel(
        self,
        process: asyncio.subprocess.Process,
        start_message: WorkerEnvelope,
        sequence: int,
    ) -> None:
        if process.returncode is not None:
            return
        try:
            await self._send(
                process,
                WorkerEnvelope(
                    kind=WorkerMessageKind.CANCEL,
                    sequence=sequence,
                    run_id=start_message.run_id,
                    source_run_id=start_message.source_run_id,
                    source_id=start_message.source_id,
                    provider_id=start_message.provider_id,
                    operation=start_message.operation,
                ),
            )
        except (BrokenPipeError, ConnectionResetError, WorkerProtocolError):
            pass

    async def _wait_or_terminate(
        self,
        process: asyncio.subprocess.Process,
        spec: WorkerProcessSpec,
        windows_job: _WindowsJob | None,
    ) -> None:
        try:
            await asyncio.wait_for(
                process.wait(), timeout=spec.graceful_shutdown_seconds
            )
        except TimeoutError:
            await self._terminate_tree(process, spec, windows_job)

    @staticmethod
    async def _terminate_tree(
        process: asyncio.subprocess.Process,
        spec: WorkerProcessSpec,
        windows_job: _WindowsJob | None,
    ) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            if windows_job is not None and not windows_job.closed:
                windows_job.close()
            else:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=spec.hard_kill_seconds)
        except TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            await process.wait()

    @staticmethod
    async def _drain_stderr(
        reader: asyncio.StreamReader,
        tail: bytearray,
        limit: int,
    ) -> None:
        raw_tail = bytearray()
        while chunk := await reader.read(4096):
            raw_tail.extend(chunk)
            if len(raw_tail) > limit * 4:
                del raw_tail[: len(raw_tail) - limit * 4]
        safe = safe_diagnostic(
            raw_tail.decode("utf-8", errors="replace"), limit=limit
        )
        tail.extend(safe.encode("utf-8"))


def safe_worker_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "COMSPEC",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({str(key): str(value) for key, value in (overrides or {}).items()})
    return environment


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    """Best-effort Job Object with kill-on-close; taskkill remains the fallback."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @property
    def closed(self) -> bool:
        return not self._handle

    @classmethod
    def attach(cls, pid: int) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        process = kernel32.OpenProcess(0x0001 | 0x0100 | 0x1000, False, pid)
        assigned = bool(process and kernel32.AssignProcessToJobObject(job, process))
        if process:
            kernel32.CloseHandle(process)
        if not configured or not assigned:
            kernel32.CloseHandle(job)
            return None
        return cls(int(job))

    def close(self) -> None:
        if not self._handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(wintypes.HANDLE(self._handle))
        self._handle = 0
