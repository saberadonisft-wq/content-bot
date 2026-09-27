from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from app.services.subtitle_jobs import SubtitleJobManager, SubtitleJobQueueFull


def test_shutdown_finalizes_done_future_before_its_callback(tmp_path, monkeypatch):
    callbacks = []
    monkeypatch.setattr(Future, "add_done_callback", lambda self, fn: callbacks.append((self, fn)))
    manager = SubtitleJobManager(tmp_path, max_cached=0)
    submitted = manager.submit("alignment", "delayed-callback", lambda _: {})
    _wait_for_state(manager, submitted["id"], {"succeeded"})
    manager.shutdown()
    assert not manager._futures and not manager._records and not manager._cancel_events
    assert not manager._last_persist_at
    for future, callback in callbacks:
        callback(future)  # A late callback is harmless after synchronous retirement.
    assert manager.get(submitted["id"])["state"] == "succeeded"


def _wait_for_state(
    manager: SubtitleJobManager,
    job_id: str,
    states: set[str],
    timeout: float = 3,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        assert job is not None
        if job["state"] in states:
            return job
        time.sleep(0.01)
    raise AssertionError(f"Job {job_id} did not reach {states}")


def test_job_progress_result_and_deduplication_are_persisted(tmp_path: Path) -> None:
    manager = SubtitleJobManager(tmp_path, max_workers=1)

    def run(context):
        context.update(42, "audio", "Đang đọc audio")
        return {"value": 7}

    first = manager.submit("alignment", "same-input", run)
    second = manager.submit("alignment", "same-input", run)
    assert first["id"] == second["id"]

    completed = _wait_for_state(manager, first["id"], {"succeeded"})
    assert completed["progress"] == 100
    assert completed["result"] == {"value": 7}
    assert (tmp_path / f"{first['id']}.json").exists()
    manager.shutdown()

    restored = SubtitleJobManager(tmp_path, max_workers=1)
    restored_job = restored.get(first["id"])
    assert restored_job is not None
    assert restored_job["state"] == "succeeded"
    assert restored_job["result"] == {"value": 7}

    def should_not_run(_context):
        raise AssertionError("A succeeded deduplicated job must be reattached")

    attached = restored.submit("alignment", "same-input", should_not_run)
    assert attached["id"] == first["id"]
    restored.shutdown()


def test_successful_ocr_cpu_fallback_does_not_mask_a_later_cuda_retry(tmp_path):
    manager = SubtitleJobManager(tmp_path, max_workers=1)
    try:
        first = manager.submit(
            "ocr",
            "same-video",
            lambda _: {
                "runtime": {
                    "requested_device": "auto",
                    "effective_device": "cpu",
                    "fallback_reason": "ocr_cuda_init_failed",
                }
            },
        )
        _wait_for_state(manager, first["id"], {"succeeded"})
    finally:
        manager.shutdown()

    restored = SubtitleJobManager(tmp_path, max_workers=1)
    try:
        retry = restored.submit(
            "ocr",
            "same-video",
            lambda _: {"runtime": {"effective_device": "cuda"}},
        )
        assert retry["id"] != first["id"]
        completed = _wait_for_state(restored, retry["id"], {"succeeded"})
        assert completed["result"]["runtime"]["effective_device"] == "cuda"

        attached = restored.submit(
            "ocr",
            "same-video",
            lambda _: pytest.fail("A successful CUDA result should still dedupe"),
        )
        assert attached["id"] == retry["id"]
    finally:
        restored.shutdown()


def test_successful_ocr_cpu_fallback_can_be_retried_in_same_manager(tmp_path):
    manager = SubtitleJobManager(tmp_path, max_workers=1)
    try:
        first = manager.submit(
            "ocr",
            "same-video",
            lambda _: {
                "runtime": {
                    "requested_device": "auto",
                    "effective_device": "cpu",
                    "fallback_reason": "ocr_cuda_init_failed",
                }
            },
        )
        _wait_for_state(manager, first["id"], {"succeeded"})

        retry = manager.submit(
            "ocr",
            "same-video",
            lambda _: {"runtime": {"effective_device": "cuda"}},
        )
        assert retry["id"] != first["id"]
        completed = _wait_for_state(manager, retry["id"], {"succeeded"})
        assert completed["result"]["runtime"]["effective_device"] == "cuda"

        attached = manager.submit(
            "ocr",
            "same-video",
            lambda _: pytest.fail("A successful CUDA result should still dedupe"),
        )
        assert attached["id"] == retry["id"]
    finally:
        manager.shutdown()


def test_running_job_can_be_canceled_cooperatively(tmp_path: Path) -> None:
    manager = SubtitleJobManager(tmp_path, max_workers=1)

    def run(context):
        context.update(10, "audio", "Đang chạy")
        while True:
            context.cancel_event.wait(0.01)
            context.raise_if_canceled()

    submitted = manager.submit("alignment", "cancel-me", run)
    _wait_for_state(manager, submitted["id"], {"running"})
    canceled_request = manager.cancel(submitted["id"])
    assert canceled_request is not None
    assert canceled_request["cancel_requested"] is True

    canceled = _wait_for_state(manager, submitted["id"], {"canceled"})
    assert canceled["result"] is None
    manager.shutdown()


def test_completed_jobs_evict_runtime_state_but_keep_result_and_dedupe(tmp_path):
    manager = SubtitleJobManager(tmp_path, max_cached=2)
    identifiers = []
    for index in range(10):
        submitted = manager.submit("alignment", str(index), lambda context: {"job_id": context.job_id})
        identifiers.append(submitted["id"])
        _wait_for_state(manager, submitted["id"], {"succeeded"})
    manager.shutdown()
    assert len(manager._records) <= 2
    assert not manager._futures and not manager._cancel_events and not manager._last_persist_at
    assert manager.get(identifiers[0])["result"] == {"job_id": identifiers[0]}
    restored = SubtitleJobManager(tmp_path, max_cached=2)
    try:
        assert len(restored._records) <= 2
        assert restored.submit("alignment", "0", lambda _: pytest.fail("Completed job must not run again"))["id"] == identifiers[0]
        assert restored.get("../outside") is None
    finally:
        restored.shutdown()


def test_history_dedupe_index_rebuild_preserves_job_files(tmp_path):
    import json

    from app.services.subtitle_jobs import SubtitleJobRecord

    originals = {}
    for index in range(4):
        record = SubtitleJobRecord(id=f"{index:020x}", kind="alignment", dedupe_key=f"old-{index}", state="succeeded", result={"index": index})
        path = tmp_path / f"{record.id}.json"
        originals[path] = json.dumps(record.snapshot()).encode()
        path.write_bytes(originals[path])
    for _ in range(2):
        manager = SubtitleJobManager(tmp_path, max_cached=0)
        try:
            assert not manager._records and not manager._dedupe
            for index in range(4):
                result = manager.submit("alignment", f"old-{index}", lambda _: pytest.fail("Legacy job must be reused"))
                assert result["id"] == f"{index:020x}"
                assert result["result"] == {"index": index}
            assert all(path.read_bytes() == content for path, content in originals.items())
            assert not (tmp_path / ".dedupe").exists()
        finally:
            manager.shutdown()
        # The index can be discarded and rebuilt entirely from legacy JSON.
        (tmp_path / ".dedupe.sqlite3").unlink()


def test_admission_limit_dedupes_active_job_and_shutdown_cancels_queued_job(tmp_path):
    manager = SubtitleJobManager(tmp_path, max_workers=1, max_pending=2)
    entered = threading.Event()

    def run(context):
        entered.set()
        context.cancel_event.wait(2)
        context.raise_if_canceled()
        return {}

    one = manager.submit("alignment", "active", run)
    assert entered.wait(1)
    two = manager.submit("alignment", "queued", run)
    assert manager.submit("alignment", "active", run)["id"] == one["id"]
    with pytest.raises(SubtitleJobQueueFull):
        manager.submit("alignment", "overflow", run)
    manager.shutdown(timeout_seconds=1)
    assert manager.get(one["id"])["state"] == "canceled"
    assert manager.get(two["id"])["state"] == "canceled"
    assert not manager._futures and not manager._cancel_events
    with pytest.raises(SubtitleJobQueueFull, match="stopping"):
        manager.submit("alignment", "new", run)


def test_shutdown_deadline_reports_non_cooperative_runner(tmp_path):
    manager = SubtitleJobManager(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def run(_context):
        entered.set()
        assert release.wait(3)
        return {}

    submitted = manager.submit("alignment", "slow", run)
    assert entered.wait(1)
    try:
        with pytest.raises(TimeoutError, match="shutdown deadline"):
            manager.shutdown(timeout_seconds=0.01)
    finally:
        release.set()
        _wait_for_state(manager, submitted["id"], {"canceled"})
