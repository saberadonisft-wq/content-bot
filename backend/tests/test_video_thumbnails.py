from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services import video_thumbnails
from app.services.video_thumbnails import (
    ThumbnailBusy,
    ThumbnailTimeout,
    VideoThumbnails,
)


@pytest.fixture(autouse=True)
def fake_executable(monkeypatch):
    monkeypatch.setattr(video_thumbnails.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")


class FakeProcess:
    def __init__(self, command, *, entered=None, release=None, timeout=False, **kwargs):
        self.output = Path(command[-1])
        self.entered, self.release = entered, release
        self.timeout = timeout
        self.returncode = None
        self.killed = False
        if entered:
            entered.set()

    def wait(self, timeout):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        if self.release:
            assert self.release.wait(3)
        if self.killed:
            self.returncode = -9
        else:
            self.output.write_bytes(b"jpeg")
            self.returncode = 0
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.release:
            self.release.set()


def test_concurrent_thumbnail_misses_share_process_and_invalidate_changed_video(tmp_path, monkeypatch):
    service = VideoThumbnails()
    source, output = tmp_path / "input.mp4", tmp_path / "thumb.jpg"
    source.write_bytes(b"original")
    entered, release = threading.Event(), threading.Event()
    processes = []

    def factory(command, **kwargs):
        process = FakeProcess(command, entered=entered, release=release, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(video_thumbnails.subprocess, "Popen", factory)
    with ThreadPoolExecutor(max_workers=8) as executor:
        pending = [executor.submit(service.get, source, output) for _ in range(8)]
        assert entered.wait(1)
        assert not output.exists()  # Final path never exposes a partially written image.
        release.set()
        assert [future.result(timeout=3) for future in pending] == [output] * 8
    assert len(processes) == 1
    assert service.get(source, output) == output
    assert len(processes) == 1
    source.write_bytes(b"replacement video is larger")
    assert service.get(source, output) == output
    assert len(processes) == 2
    assert not service._flights and not service._processes
    assert not list(tmp_path.glob("*.part.*"))


def test_thumbnail_timeout_kills_process_and_cleans_partial_files(tmp_path, monkeypatch):
    source, output = tmp_path / "input.mp4", tmp_path / "thumb.jpg"
    source.write_bytes(b"video")
    processes = []

    def factory(command, **kwargs):
        process = FakeProcess(command, timeout=True, **kwargs)
        process.output.write_bytes(b"partial")
        processes.append(process)
        return process

    monkeypatch.setattr(video_thumbnails.subprocess, "Popen", factory)
    service = VideoThumbnails(timeout_seconds=0.01)
    with pytest.raises(ThumbnailTimeout):
        service.get(source, output)
    assert processes[0].killed
    assert not output.exists() and not service._processes and not service._flights
    assert not list(tmp_path.glob("*.part.*"))


def test_thumbnail_admission_and_shutdown_are_bounded(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(video_thumbnails.subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, entered=entered, release=release, **kwargs))
    service = VideoThumbnails(concurrency=1, max_pending=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.get, source, tmp_path / "one.jpg")
        assert entered.wait(1)
        with pytest.raises(ThumbnailBusy, match="queue is full"):
            service.get(source, tmp_path / "two.jpg")
        service.shutdown()
        with pytest.raises(video_thumbnails.ThumbnailError):
            pending.result(timeout=3)
    with pytest.raises(ThumbnailBusy, match="stopping"):
        service.get(source, tmp_path / "three.jpg")
