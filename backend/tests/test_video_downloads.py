from __future__ import annotations

import io
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.services import video_download_worker, video_downloads
from app.services.subtitle_jobs import SubtitleJobQueueFull
from app.services.video_downloads import VideoDownloadManager, normalize_video_url


def settled(manager, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = next(record for record in manager.list() if record["id"] == job_id)
        if job["state"] not in {"queued", "running"} and job_id not in manager._futures:
            return job
        time.sleep(0.01)
    pytest.fail("Download job did not finish")


class Worker:
    """Exercises the real queue and disk publishing without contacting platforms."""
    def __init__(self, *, release=None, entered=None, fail=False):
        self.stdin = io.StringIO()
        self.stdin.close = self.accept_input
        self.stdout = self
        self.returncode = None
        self.release = release
        self.entered = entered
        self.fail = fail
        self.cookie_text = None
        self.payload = None
        self.previous_bytes = b""

    def accept_input(self):
        if self.payload is not None:
            return
        self.payload = json.loads(self.stdin.getvalue())
        if self.payload.get("cookie_file"):
            self.cookie_text = Path(self.payload["cookie_file"]).read_text(encoding="utf-8")

    def __iter__(self):
        path = Path(self.payload["directory"]) / "video.mp4"
        self.previous_bytes = path.read_bytes() if path.exists() else b""
        path.write_bytes(b"partial")
        if self.entered:
            self.entered.set()
        if self.release:
            assert self.release.wait(3)
        if self.returncode is not None:
            return
        if self.fail:
            self.returncode = 1
            yield json.dumps({"event": "error", "message": "Video không khả dụng."})
            return
        yield json.dumps({"event": "progress", "phase": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "title": "Video thử nghiệm"})
        path.write_bytes(b"complete video")
        self.returncode = 0
        yield json.dumps({"event": "complete", "filename": path.name, "title": "Video thử nghiệm", "duration": 12.5})

    def close(self):
        pass

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        if self.release:
            self.release.set()


@pytest.fixture
def workers(monkeypatch):
    created = []
    options = {}

    def create(*args, **kwargs):
        worker = Worker(**options)
        created.append(worker)
        return worker

    monkeypatch.setattr(video_downloads.subprocess, "Popen", create)
    monkeypatch.setattr(VideoDownloadManager, "_kill", staticmethod(lambda process: process.kill() if process.poll() is None else None))
    return created, options


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://127.0.0.1/video", "http://192.168.1.1/video",
    "http://localhost/video", "https://example.local/video", "https://user:pass@bilibili.com/video/x",
    "https://bilibili.com:9000/video/x", "https://bilibili.com/\nvideo", "not a link",
])
def test_rejects_invalid_or_internal_links(url):
    with pytest.raises(ValueError):
        normalize_video_url(url)


def test_link_normalization_preserves_bilibili_part():
    assert normalize_video_url(" https://WWW.BILIBILI.COM/video/BV1?p=2#tracking ") == "https://www.bilibili.com/video/BV1?p=2"


def test_success_deduplication_metadata_restart_and_deleted_file(tmp_path, workers):
    created, _ = workers
    manager = VideoDownloadManager(tmp_path)
    url = "https://www.bilibili.com/video/BV1test?p=2"
    try:
        job = manager.submit([url, url])[0]
        result = settled(manager, job["id"])
        assert result["state"] == "succeeded"
        assert result["title"] == "Video thử nghiệm"
        assert (manager.upload_dir / result["filename"]).read_bytes() == b"complete video"
        assert manager.metadata()[job["id"]]["duration"] == 12.5
        assert manager.submit([url])[0]["id"] == job["id"]
        assert len(created) == 1
    finally:
        manager.shutdown()
    restarted = VideoDownloadManager(tmp_path)
    try:
        assert restarted.submit([url])[0]["id"] == job["id"]
        (restarted.upload_dir / result["filename"]).unlink()
        new_job = restarted.submit([url])[0]
        assert new_job["id"] != job["id"]
        assert settled(restarted, new_job["id"])["state"] == "succeeded"
    finally:
        restarted.shutdown()


def test_cancel_active_and_queued_never_publishes_and_cleans_cookies(tmp_path, workers):
    created, options = workers
    entered, release = threading.Event(), threading.Event()
    options.update(entered=entered, release=release)
    manager = VideoDownloadManager(tmp_path, max_workers=1)
    cookie_text = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\ttest-secret\n"
    try:
        jobs = manager.submit(["https://b23.tv/abc", "https://b23.tv/def"], cookie_text=cookie_text)
        assert entered.wait(1)
        assert not manager.upload_dir.exists()
        manager.cancel(jobs[1]["id"])
        manager.cancel(jobs[0]["id"])
        assert settled(manager, jobs[0]["id"])["state"] == "canceled"
        assert settled(manager, jobs[1]["id"])["state"] == "canceled"
        assert len(created) == 1
        assert created[0].cookie_text == cookie_text
        work = tmp_path / "downloads" / "work" / jobs[0]["id"]
        assert (work / "video.mp4").read_bytes() == b"partial"
        assert not list(work.glob("cookies*.txt"))
        assert "test-secret" not in "".join(path.read_text() for path in manager.jobs_dir.glob("*.json"))
        assert not manager.upload_dir.exists()
    finally:
        release.set()
        manager.shutdown()


def test_failure_can_retry_and_shutdown_stops_admission(tmp_path, workers):
    created, options = workers
    options["fail"] = True
    manager = VideoDownloadManager(tmp_path)
    try:
        first = manager.submit(["https://b23.tv/abc"])[0]
        assert settled(manager, first["id"])["state"] == "failed"
        assert (manager.work_root / first["id"] / "video.mp4").read_bytes() == b"partial"
        options["fail"] = False
        second = manager.submit([first["url"]])[0]
        assert second["id"] == first["id"]
        assert settled(manager, second["id"])["state"] == "succeeded"
        assert created[-1].previous_bytes == b"partial"
        assert len(manager.list()) == 1
    finally:
        manager.shutdown()
    with pytest.raises(SubtitleJobQueueFull):
        manager.submit([first["url"]])


def test_batch_validation_and_queue_capacity_are_atomic(tmp_path, workers):
    _, options = workers
    entered, release = threading.Event(), threading.Event()
    options.update(entered=entered, release=release)
    manager = VideoDownloadManager(tmp_path, max_pending=1)
    try:
        with pytest.raises(ValueError):
            manager.submit(["https://b23.tv/abc", "file:///x"])
        assert manager.list() == []
        manager.submit(["https://b23.tv/abc"])
        assert entered.wait(1)
        with pytest.raises(SubtitleJobQueueFull):
            manager.submit(["https://b23.tv/def"])
        assert len(manager.list()) == 1
    finally:
        manager.shutdown()


def test_worker_blocks_private_dns_results_including_mixed_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    video_download_worker.guard_public_network()
    with pytest.raises(OSError, match="nội bộ"):
        socket.getaddrinfo("redirect.example", 443)


def test_download_api_library_metadata_and_delete(tmp_path, workers, application_services, monkeypatch):
    manager = VideoDownloadManager(tmp_path / "videos")
    monkeypatch.setattr(application_services, "video_downloads", manager)
    monkeypatch.setattr(main.settings, "content_bot_data_dir", tmp_path)
    monkeypatch.setattr(main.settings, "content_bot_auth_enabled", False)
    client = TestClient(main.app)
    response = client.post("/api/v1/videos/downloads", json={"urls": ["https://b23.tv/abc"]})
    assert response.status_code == 202
    job = settled(manager, response.json()[0]["id"])
    assert client.get("/api/v1/videos/downloads").json()[0]["state"] == "succeeded"
    video = next(video for video in client.get("/api/v1/videos").json() if video["id"] == job["id"])
    assert video["downloaded"] is True
    assert video["title"] == "Video thử nghiệm" and video["platform"] == "Bilibili"
    assert video["type"] == "original"  # Existing subtitle/video routes remain compatible.
    assert client.get(video["video_url"]).status_code == 200
    assert client.delete(f"/api/v1/videos/{job['id']}?type=original").status_code == 204
    assert client.post("/api/v1/videos/downloads", json={"urls": ["http://127.0.0.1/x"]}).status_code == 422
    assert client.post("/api/v1/videos/downloads", json={"urls": ["https://b23.tv/a"], "cookie_text": "bad"}).status_code == 422
    assert client.post("/api/v1/videos/downloads/no-such-job/cancel").status_code == 404
    assert client.post("/api/v1/videos/downloads/no-such-job/retry", json={}).status_code == 404
    retry = client.post(f"/api/v1/videos/downloads/{job['id']}/retry", json={})
    assert retry.status_code == 202 and retry.json()["id"] == job["id"]
    assert settled(manager, job["id"])["state"] == "succeeded"


def test_interrupted_records_resume_on_start(tmp_path, workers):
    manager = VideoDownloadManager(tmp_path)
    job = manager.submit(["https://b23.tv/abc"])[0]
    result = settled(manager, job["id"])
    manager.shutdown()
    result.update(state="running", filename="")
    (manager.upload_dir / f"{job['id']}.mp4").unlink()
    (manager.jobs_dir / f"{job['id']}.json").write_text(json.dumps(result), encoding="utf-8")
    restarted = VideoDownloadManager(tmp_path)
    try:
        assert restarted.list()[0]["state"] == "paused"
        assert restarted.list()[0]["phase"] == "interrupted"
        restarted.start()
        assert settled(restarted, job["id"])["state"] == "succeeded"
        assert len(restarted.list()) == 1
    finally:
        restarted.shutdown()


def test_shutdown_preserves_partial_bytes_then_start_resumes_same_job(tmp_path, workers):
    created, options = workers
    entered, release = threading.Event(), threading.Event()
    options.update(entered=entered, release=release)
    manager = VideoDownloadManager(tmp_path)
    job = manager.submit(["https://b23.tv/resume"])[0]
    assert entered.wait(1)
    manager.shutdown()
    assert manager.list()[0]["state"] == "paused"
    partial = manager.work_root / job["id"] / "video.mp4"
    assert partial.read_bytes() == b"partial"
    options.clear()
    restarted = VideoDownloadManager(tmp_path)
    try:
        assert len(created) == 1  # Recovery starts only with the application lifecycle.
        restarted.start()
        restarted.start()
        assert settled(restarted, job["id"])["state"] == "succeeded"
        assert len(created) == 2 and created[-1].previous_bytes == b"partial"
        assert not partial.exists()
    finally:
        restarted.shutdown()


def test_cookie_job_keeps_bytes_but_waits_for_credentials_after_restart(tmp_path, workers):
    created, options = workers
    entered, release = threading.Event(), threading.Event()
    options.update(entered=entered, release=release)
    cookie_text = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsecret\n"
    manager = VideoDownloadManager(tmp_path)
    job = manager.submit(["https://b23.tv/cookies"], cookie_text=cookie_text)[0]
    assert entered.wait(1)
    manager.shutdown()
    options.clear()
    restarted = VideoDownloadManager(tmp_path)
    try:
        restarted.start()
        assert restarted.list()[0]["phase"] == "needs_cookies"
        assert len(created) == 1
        assert not list(restarted.work_root.rglob("cookies*.txt"))
        with pytest.raises(ValueError, match="cookies"):
            restarted.retry(job["id"])
        assert restarted.retry(job["id"], cookie_text)["id"] == job["id"]
        assert settled(restarted, job["id"])["state"] == "succeeded"
        assert created[-1].previous_bytes == b"partial"
    finally:
        restarted.shutdown()


def test_explicitly_canceled_jobs_do_not_restart_automatically(tmp_path, workers):
    created, options = workers
    entered, release = threading.Event(), threading.Event()
    options.update(entered=entered, release=release)
    manager = VideoDownloadManager(tmp_path)
    job = manager.submit(["https://b23.tv/cancel"])[0]
    assert entered.wait(1)
    manager.cancel(job["id"])
    settled(manager, job["id"])
    manager.shutdown()
    restarted = VideoDownloadManager(tmp_path)
    try:
        restarted.start()
        assert len(created) == 1
        assert restarted.list()[0]["state"] == "canceled"
    finally:
        restarted.shutdown()
