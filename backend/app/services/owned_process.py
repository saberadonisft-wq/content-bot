"""Own one child process tree; never discover or kill unrelated processes."""

import ctypes
import os
import signal
import subprocess
import time
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("ProcessUserTimeLimit", ctypes.c_int64),
        ("JobUserTimeLimit", ctypes.c_int64),
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
        (name, ctypes.c_uint64)
        for name in [
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        ]
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters)] + [
        (name, ctypes.c_size_t)
        for name in [
            "ProcessMemoryLimit",
            "JobMemoryLimit",
            "PeakProcessMemoryUsed",
            "PeakJobMemoryUsed",
        ]
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_int64)
        for name in [
            "TotalUserTime",
            "TotalKernelTime",
            "ThisPeriodTotalUserTime",
            "ThisPeriodTotalKernelTime",
        ]
    ] + [
        (name, wintypes.DWORD)
        for name in [
            "TotalPageFaultCount",
            "TotalProcesses",
            "ActiveProcesses",
            "TotalTerminatedProcesses",
        ]
    ]


class OwnedProcess:
    """Windows children wait for supervisor readiness before starting descendant processes."""

    def __init__(self, command, **kwargs):
        self.job = None
        if os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            self.kernel.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.job = self.kernel.CreateJobObjectW(None, None)
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000
            if not self.job or not self.kernel.SetInformationJobObject(
                self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                error = ctypes.get_last_error()
                if self.job:
                    self.kernel.CloseHandle(self.job)
                raise ctypes.WinError(error)
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
            )
        else:
            kwargs["start_new_session"] = True
        try:
            self.process = subprocess.Popen(command, **kwargs)
        except BaseException:
            if self.job:
                self.kernel.CloseHandle(self.job)
            raise
        if self.job and not self.kernel.AssignProcessToJobObject(
            self.job, wintypes.HANDLE(int(self.process._handle))
        ):
            error = ctypes.get_last_error()
            self.process.kill()
            self.process.wait(timeout=5)
            self.kernel.CloseHandle(self.job)
            self.job = None
            raise ctypes.WinError(error)

    def close(self):
        pid = self.process.pid
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                counts = _Accounting()
                if not self.kernel.QueryInformationJobObject(
                    self.job, 1, ctypes.byref(counts), ctypes.sizeof(counts), None
                ):
                    break
                if counts.ActiveProcesses == 0:
                    break
                time.sleep(0.02)
            self.kernel.CloseHandle(self.job)
            self.job = None
            # A descendant can briefly survive the Job Object termination on
            # Windows (and keep inherited stdout/stderr handles open).  The
            # PID tree is still owned by this supervisor, so use taskkill as
            # a bounded fallback before temporary job files are removed.
            if self.process.poll() is None:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=2,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
