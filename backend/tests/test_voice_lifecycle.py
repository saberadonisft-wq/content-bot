import subprocess
import sys
import threading

import pytest

from app.services.subtitle_jobs import SubtitleJobQueueFull
from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.models import VoiceDocument
from app.services.voiceover.store import VoiceStore, write_json


def test_voice_admission_queued_cancel_and_forced_process_cleanup(tmp_path, monkeypatch):
    store = VoiceStore(tmp_path)
    manager = VoiceManager(store, max_pending=2)
    entered = threading.Event()
    processes = []
    monkeypatch.setattr(manager, "status", lambda: {"ready": True, "devices": ["cpu"]})
    monkeypatch.setattr(manager, "manifest", lambda *args: {"clips": []})

    def run(owner, job, manifest):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        with store.lock:
            manager._processes[job["id"]] = process
        entered.set()
        try:
            process.wait(timeout=10)
            job.update(state="canceled")
            write_json(store.path(owner, "jobs", job["id"]), job)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            with store.lock:
                manager._processes.pop(job["id"], None)

    monkeypatch.setattr(manager, "_run", run)
    projects = [str(index) * 20 for index in (1, 2, 3)]
    for project in projects:
        store.save_document("owner", VoiceDocument(project_id=project, video_fingerprint="fixture", clips=[],
                                                  profile={"id": "test", "name": "Test", "preset": "test"}))
    try:
        one = manager.start("owner", projects[0], "cpu")
        assert entered.wait(2)
        two = manager.start("owner", projects[1], "cpu")
        assert manager.start("owner", projects[0], "cpu")["id"] == one["id"]
        with pytest.raises(SubtitleJobQueueFull):
            manager.start("owner", projects[2], "cpu")
        manager.shutdown(timeout_seconds=0.01)
        assert len(processes) == 1 and processes[0].poll() is not None
        assert manager.get("owner", two["id"])["state"] == "canceled"
        assert not manager.live and not manager.controls and not manager._futures and not manager._processes
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        manager.shutdown()
