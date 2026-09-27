"""Windows runtime robustness, orphaned process cleanup, and hardware diagnostics."""
from __future__ import annotations

import logging
import platform
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock
from typing import Any

logger = logging.getLogger("content_bot.system_hygiene")


@dataclass(frozen=True)
class BrowserProcessOwnership:
    """Identity recorded when the app starts a managed browser process."""

    pid: int
    creation_time: str
    parent_pid: int | None = None
    session_id: int | None = None


class BrowserProcessOwnershipRegistry:
    """Thread-safe registry of identities captured by managed browser drivers.

    The registry intentionally stores process identity, not a profile path. A
    profile is only a lookup hint and can be shared by an unrelated process;
    PID plus creation time (and, when available, parent/session) is the
    authorization record used by cleanup.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[tuple[int, str], BrowserProcessOwnership] = {}

    def register(self, ownership: BrowserProcessOwnership) -> None:
        if ownership.pid <= 0 or not ownership.creation_time:
            raise ValueError("Browser process ownership identity is incomplete")
        with self._lock:
            self._records[(ownership.pid, ownership.creation_time)] = ownership

    def unregister(self, ownership: BrowserProcessOwnership) -> None:
        with self._lock:
            self._records.pop((ownership.pid, ownership.creation_time), None)

    def snapshot(self) -> tuple[BrowserProcessOwnership, ...]:
        with self._lock:
            return tuple(self._records.values())

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


BROWSER_PROCESS_OWNERSHIP = BrowserProcessOwnershipRegistry()


def cleanup_orphaned_browser_processes(
    profile_pattern: str = "content_bot_browser_profile",
    *,
    ownership: Iterable[BrowserProcessOwnership] | None = None,
    terminate: bool = False,
) -> int:
    """Report or terminate only browser processes owned by this app.

    A profile substring is only a filter. It is never sufficient authorization
    to terminate a process. Callers must pass identities captured at process
    creation; the default is report-only so startup cannot kill an unrelated
    browser after a PID has been reused.
    """
    if platform.system() != "Windows":
        return 0
    records = list(
        BROWSER_PROCESS_OWNERSHIP.snapshot() if ownership is None else ownership
    )
    if not records:
        logger.info("Skipping browser cleanup: no owned process identities supplied")
        return 0

    killed = 0
    try:
        # Return identity fields as JSON so PID reuse can be rejected before kill.
        safe_pattern = profile_pattern.replace("'", "''")
        ps_cmd = (
            f"Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.Name -match 'chrome|edge|coc_coc' -and $_.CommandLine -like '*{safe_pattern}*' }} | "
            f"Select-Object ProcessId,ParentProcessId,SessionId,CreationDate,CommandLine | ConvertTo-Json -Compress"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return 0
        import json

        payload = json.loads(res.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        current = {
            int(row["ProcessId"]): row
            for row in rows
            if isinstance(row, dict) and str(row.get("ProcessId", "")).isdigit()
        }
        for record in records:
            row = current.get(record.pid)
            if not row or str(row.get("CreationDate", "")) != record.creation_time:
                logger.info("Skipping PID %d: process identity no longer matches", record.pid)
                continue
            if record.parent_pid is not None and int(row.get("ParentProcessId", -1)) != record.parent_pid:
                logger.info("Skipping PID %d: parent identity no longer matches", record.pid)
                continue
            if record.session_id is not None and int(row.get("SessionId", -1)) != record.session_id:
                logger.info("Skipping PID %d: session identity no longer matches", record.pid)
                continue
            if not terminate:
                logger.info("Owned browser PID %d is eligible for cleanup (report-only)", record.pid)
                continue
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(record.pid)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                killed += 1
                logger.info("Cleaned up owned browser process PID %d", record.pid)

    except Exception as exc:
        logger.debug("Orphaned browser cleanup error: %s", exc)

    return killed


def verify_onnx_runtime_health() -> dict[str, Any]:
    """Diagnose ONNX Runtime installation and check for CUDA provider availability."""
    info: dict[str, Any] = {
        "installed": False,
        "version": None,
        "providers": [],
        "has_cuda": False,
        "warning": None,
    }

    try:
        import onnxruntime as ort

        info["installed"] = True
        info["version"] = getattr(ort, "__version__", "unknown")
        providers = ort.get_available_providers()
        info["providers"] = providers
        info["has_cuda"] = "CUDAExecutionProvider" in providers

        # Check if NVIDIA GPU exists on Windows
        has_nvidia_gpu = False
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            has_nvidia_gpu = res.returncode == 0 and len(res.stdout.strip()) > 0
        except Exception as exc:
            logger.debug("Could not query NVIDIA runtime: %s", exc)

        if has_nvidia_gpu and not info["has_cuda"]:
            info["warning"] = (
                "Máy có card NVIDIA nhưng ONNX Runtime đang chạy bản CPU (thiếu CUDAExecutionProvider). "
                "Có thể gói rapidocr-onnxruntime đã ghi đè onnxruntime-gpu. "
                "Khắc phục: pip install --no-deps onnxruntime-gpu"
            )

    except Exception as exc:
        info["warning"] = f"Không thể nạp onnxruntime: {exc}"

    return info


def lower_current_process_priority() -> bool:
    """Lower the current process priority to BELOW_NORMAL to prevent UI freezes on Windows."""
    if platform.system() != "Windows":
        return False

    try:
        import ctypes

        # SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS (0x00004000))
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        success = ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        return bool(success)
    except Exception as exc:
        logger.debug("Failed to set process priority: %s", exc)
        return False
