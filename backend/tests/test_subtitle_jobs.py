from __future__ import annotations

import time
from pathlib import Path

from app.services.subtitle_jobs import SubtitleJobManager


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
