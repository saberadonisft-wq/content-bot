"""Real process ownership and cancellation tests; no model download or paid API."""

import os
import sys
import time
from pathlib import Path

import pytest

from app.services.owned_process import OwnedProcess
from app.services.subtitle_asr import SubtitleAsrError
from app.services.subtitle_asr_supervisor import run_subtitle_asr_job
from app.services.subtitle_jobs import SubtitleJobCanceled


def is_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_worker_returns_actionable_model_error_without_network(tmp_path):
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    with pytest.raises(
        SubtitleAsrError, match="không hợp lệ|not.*supported|Invalid model"
    ):
        run_subtitle_asr_job(
            video,
            {"duration_ms": 1000, "has_audio": True},
            device="cpu",
            model_name="__fixture_no_such_model__",
            allow_download=False,
        )


def test_cancel_supervisor_terminates_owned_descendant(monkeypatch, tmp_path):
    import app.services.subtitle_asr_supervisor as supervisor

    observed = {}

    def fixture_process(command, **kwargs):
        root = Path(command[-1])
        observed["root"] = root
        script = """
import pathlib,subprocess,sys,time
root=pathlib.Path(sys.argv[1])
while not (root/'ready').exists():time.sleep(.02)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
(root/'descendant.pid').write_text(str(child.pid))
time.sleep(60)
"""
        child = OwnedProcess([sys.executable, "-c", script, str(root)], **kwargs)
        observed["pid"] = child.process.pid
        return child

    monkeypatch.setattr(supervisor, "OwnedProcess", fixture_process)

    class Context:
        def raise_if_canceled(self):
            if "root" in observed:
                marker = observed["root"] / "descendant.pid"
                if marker.exists():
                    observed["descendant"] = int(marker.read_text())
                    observed["cancel_time"] = time.monotonic()
                    raise SubtitleJobCanceled("fixture canceled")

        def update(self, *args):
            pass

    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    with pytest.raises(SubtitleJobCanceled, match="fixture canceled"):
        run_subtitle_asr_job(video, {"duration_ms": 1000}, context=Context())
    assert time.monotonic() - observed["cancel_time"] < 5
    assert not is_running(observed["pid"])
    deadline = time.monotonic() + 3
    while is_running(observed["descendant"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not is_running(observed["descendant"])
