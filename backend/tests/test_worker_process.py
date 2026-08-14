from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.crawlers.runtime import (
    WorkerControl,
    WorkerEnvelope,
    WorkerExited,
    WorkerMessageKind,
    WorkerProcessSpec,
    WorkerProcessSupervisor,
    WorkerProtocolError,
    safe_worker_environment,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FAKE_WORKER = BACKEND_ROOT / "tests" / "fixtures" / "fake_cbce_worker.py"


def start_message(scenario: str) -> WorkerEnvelope:
    return WorkerEnvelope(
        kind=WorkerMessageKind.START,
        sequence=0,
        run_id="run-1",
        source_run_id="source-run-1",
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="search",
        payload={"scenario": scenario, "credential_ref": "vault://test-account"},
    )


def process_spec(**overrides: object) -> WorkerProcessSpec:
    values = {
        "command": (sys.executable, str(FAKE_WORKER)),
        "cwd": BACKEND_ROOT,
        "environment": safe_worker_environment(
            {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        ),
        "ready_timeout_seconds": 2,
        "heartbeat_timeout_seconds": 2,
        "graceful_shutdown_seconds": 0.2,
        "hard_kill_seconds": 2,
    }
    values.update(overrides)
    return WorkerProcessSpec(**values)


def test_worker_process_success_is_versioned_ordered_and_redacted() -> None:
    async def run():
        messages = []

        async def collect(message: WorkerEnvelope) -> None:
            messages.append(message)

        result = await WorkerProcessSupervisor().execute(
            process_spec(), start_message("success"), on_message=collect
        )
        return result, messages

    result, messages = asyncio.run(run())

    assert result.terminal_kind is WorkerMessageKind.COMPLETE
    assert result.returncode == 0
    assert result.message_count == 4
    assert [message.sequence for message in messages] == [1, 2, 3, 4]
    assert "worker-secret" not in result.stderr_tail
    assert "abcdefghijklmnop" not in result.stderr_tail
    assert "user:pass" not in result.stderr_tail
    assert "[REDACTED]" in result.stderr_tail


def test_worker_process_discards_duplicate_sequences() -> None:
    async def run():
        messages = []

        async def collect(message: WorkerEnvelope) -> None:
            messages.append(message)

        result = await WorkerProcessSupervisor().execute(
            process_spec(), start_message("duplicate"), on_message=collect
        )
        return result, messages

    result, messages = asyncio.run(run())
    assert result.message_count == 2
    assert [message.kind for message in messages] == [
        WorkerMessageKind.RUN_STARTED,
        WorkerMessageKind.COMPLETE,
    ]


def test_worker_error_and_invalid_stdout_are_typed_and_cleanup_owned_process() -> None:
    async def run_error() -> WorkerExited:
        with pytest.raises(WorkerExited) as captured:
            await WorkerProcessSupervisor().execute(
                process_spec(), start_message("error"), on_message=lambda _: asyncio.sleep(0)
            )
        return captured.value

    error = asyncio.run(run_error())
    assert error.returncode == 0
    assert "worker-secret" not in error.stderr_tail

    async def run_invalid() -> None:
        with pytest.raises((WorkerProtocolError, ValueError)):
            await WorkerProcessSupervisor().execute(
                process_spec(),
                start_message("invalid_stdout"),
                on_message=lambda _: asyncio.sleep(0),
            )

    asyncio.run(run_invalid())


def test_worker_control_cancel_reaches_worker_over_stdin() -> None:
    async def run():
        control = WorkerControl()
        messages = []

        async def collect(message: WorkerEnvelope) -> None:
            messages.append(message)

        task = asyncio.create_task(
            WorkerProcessSupervisor().execute(
                process_spec(),
                start_message("wait_cancel"),
                on_message=collect,
                control=control,
            )
        )
        while not messages:
            await asyncio.sleep(0.01)
        await control.cancel()
        return await task, messages

    result, messages = asyncio.run(run())
    assert result.terminal_kind is WorkerMessageKind.CANCELLED
    assert messages[-1].kind is WorkerMessageKind.CANCELLED


@pytest.mark.skipif(os.name != "nt", reason="Windows task-tree ownership regression")
def test_cancelled_parent_hard_kills_descendant_process_tree_on_windows() -> None:
    async def run() -> int:
        child_pid = 0
        child_seen = asyncio.Event()

        async def collect(message: WorkerEnvelope) -> None:
            nonlocal child_pid
            if "child_pid" in message.payload:
                child_pid = int(message.payload["child_pid"])
                child_seen.set()

        task = asyncio.create_task(
            WorkerProcessSupervisor().execute(
                process_spec(),
                start_message("ignore_cancel_with_child"),
                on_message=collect,
            )
        )
        await asyncio.wait_for(child_seen.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return child_pid

    child_pid = asyncio.run(run())
    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    assert str(child_pid) not in listing


@pytest.mark.parametrize(
    "command",
    [
        ("python", "worker.py", "--cookies", "session=secret"),
        ("python", "worker.py", "--access-token=secret"),
    ],
)
def test_worker_spec_rejects_secrets_in_argv(command: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="argv"):
        process_spec(command=command)


def test_worker_spec_rejects_secret_environment_values() -> None:
    with pytest.raises(ValueError, match="environment"):
        process_spec(environment={"API_KEY": "secret"})
