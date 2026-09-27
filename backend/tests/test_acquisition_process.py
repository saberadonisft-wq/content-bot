import json
import subprocess
import threading

import pytest

from app.services import acquisition_process
from app.services.acquisition import AcquisitionError


class _TimedProcess:
    pid = 4242

    def __init__(self, *, response=None, started=None):
        self.returncode = None
        self._response = response
        self._started = started
        self.calls = 0

    def communicate(self, input=None, timeout=None):
        del input, timeout
        self.calls += 1
        if self._started is not None:
            self._started.set()
        if self.calls == 1:
            raise subprocess.TimeoutExpired("worker", 0.25)
        self.returncode = 0
        return json.dumps(self._response or {"items": []}), ""

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_metadata_worker_returns_protocol_items_after_a_timeout(monkeypatch):
    process = _TimedProcess(response={"items": [{"id": "video-1"}]})
    killed = []
    monkeypatch.setattr(acquisition_process.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        acquisition_process.VideoDownloadManager,
        "_kill",
        staticmethod(lambda value: killed.append(value) or value.kill()),
    )

    result = acquisition_process.extract_metadata(
        {"limits": {"deadline_seconds": 1}}, threading.Event()
    )

    assert result == [{"id": "video-1"}]
    assert process.calls >= 2
    assert killed == [process]


def test_metadata_worker_is_hard_stopped_on_cancellation(monkeypatch):
    started = threading.Event()
    cancellation = threading.Event()
    process = _TimedProcess(started=started)
    killed = []
    monkeypatch.setattr(acquisition_process.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        acquisition_process.VideoDownloadManager,
        "_kill",
        staticmethod(lambda value: killed.append(value) or value.kill()),
    )

    def cancel_after_worker_start():
        assert started.wait(1)
        cancellation.set()

    thread = threading.Thread(target=cancel_after_worker_start)
    thread.start()
    result = acquisition_process.extract_metadata(
        {"limits": {"deadline_seconds": 1}}, cancellation
    )
    thread.join(timeout=1)

    assert result == []
    assert killed == [process]


def test_metadata_worker_reports_deadline_and_kills_child(monkeypatch):
    class NeverEndingProcess(_TimedProcess):
        def communicate(self, input=None, timeout=None):
            del input, timeout
            self.calls += 1
            raise subprocess.TimeoutExpired("worker", 0.01)

    process = NeverEndingProcess()
    killed = []
    monkeypatch.setattr(acquisition_process.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        acquisition_process.VideoDownloadManager,
        "_kill",
        staticmethod(lambda value: killed.append(value) or value.kill()),
    )

    with pytest.raises(AcquisitionError) as caught:
        acquisition_process.extract_metadata(
            {"limits": {"deadline_seconds": 0.01}}, threading.Event()
        )

    assert caught.value.code == "DEADLINE_EXCEEDED"
    assert killed == [process]
