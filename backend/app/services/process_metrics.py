"""Small, optional process metrics used by bounded worker benchmarks."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from pathlib import Path


def process_rss_bytes(pid: int | None = None) -> int | None:
    """Return current resident memory for a process when the OS exposes it."""
    pid = pid or os.getpid()
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("page_fault_count", wintypes.DWORD),
                    ("peak_working_set_size", ctypes.c_size_t),
                    ("working_set_size", ctypes.c_size_t),
                    ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                    ("quota_paged_pool_usage", ctypes.c_size_t),
                    ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                    ("quota_non_paged_pool_usage", ctypes.c_size_t),
                    ("pagefile_usage", ctypes.c_size_t),
                    ("peak_pagefile_usage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
            if not handle:
                return None
            try:
                counters = _Counters()
                counters.cb = ctypes.sizeof(counters)
                if not psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb
                ):
                    return None
                return int(counters.working_set_size)
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError, ctypes.ArgumentError):
            return None

    status = Path(f"/proc/{pid}/status")
    try:
        match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", status.read_text(), re.MULTILINE)
    except OSError:
        return None
    return int(match.group(1)) * 1024 if match else None


def nvidia_memory_snapshot() -> list[dict[str, int]]:
    """Read GPU memory only when nvidia-smi is available; never raises."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    values: list[dict[str, int]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            values.append(
                {
                    "index": int(parts[0]),
                    "used_bytes": int(parts[1]) * 1024 * 1024,
                    "total_bytes": int(parts[2]) * 1024 * 1024,
                }
            )
        except ValueError:
            continue
    return values
