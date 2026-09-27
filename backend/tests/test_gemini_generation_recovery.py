import asyncio
import json
import threading
import time

import pytest
from test_gemini_pipeline import cue, pipeline_fixture

from app.api import subtitles as api
from app.schemas import GeminiSubtitleRequest
from app.services.subtitle_jobs import SubtitleJobManager, SubtitleJobRecord


def wait_for(manager, job_id, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if predicate(job):
            return job
        time.sleep(0.01)
    raise AssertionError(manager.get(job_id))


def save_job(root, **kwargs):
    record = SubtitleJobRecord(**kwargs)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{record.id}.json").write_text(json.dumps(record.snapshot()), encoding="utf-8")
    return record.id


def test_shutdown_preserves_active_and_queued_intent_but_not_user_cancellation(tmp_path):
    manager = SubtitleJobManager(tmp_path, recoverable_kinds=("generation",))
    entered = threading.Event()

    def run(context):
        context.update_details({"completed": 20, "total": 68})
        context.update(33, "generating", "Working")
        entered.set()
        context.cancel_event.wait(3)
        context.raise_if_canceled()
        return {}

    first = manager.submit("generation", "active", run)
    assert entered.wait(2)
    second = manager.submit("generation", "queued", run)
    canceled = manager.submit("generation", "user-cancel", run)
    manager.cancel(canceled["id"])
    manager.shutdown()
    assert manager.get(first["id"])["phase"] == "interrupted"
    assert manager.get(first["id"])["progress"] == 33
    assert manager.get(first["id"])["details"]["completed"] == 20
    assert not manager.get(first["id"])["cancel_requested"]
    assert manager.get(second["id"])["phase"] == "interrupted"
    assert manager.get(canceled["id"])["state"] == "canceled"

    restored = SubtitleJobManager(tmp_path, recoverable_kinds=("generation",))
    calls = []
    try:
        def factory(job):
            return lambda context: calls.append(context.job_id) or {"restored": True}
        restored.recover_interrupted(factory)
        restored.recover_interrupted(factory)
        for job in (first, second):
            assert wait_for(restored, job["id"], lambda row: row["state"] == "succeeded")["result"] == {"restored": True}
        assert calls == [first["id"], second["id"]]
    finally:
        restored.shutdown()


def test_recovery_ignores_superseded_failed_canceled_and_other_job_kinds(tmp_path):
    save_job(tmp_path, id="a" * 20, kind="generation", dedupe_key="retry", state="failed", phase="interrupted", created_at="2026-01-01")
    save_job(tmp_path, id="b" * 20, kind="generation", dedupe_key="retry", state="succeeded", created_at="2026-01-02")
    save_job(tmp_path, id="c" * 20, kind="generation", dedupe_key="auth-failure", state="failed", phase="failed")
    save_job(tmp_path, id="d" * 20, kind="generation", dedupe_key="user-cancel", state="running", cancel_requested=True)
    save_job(tmp_path, id="e" * 20, kind="review", dedupe_key="review", state="running")
    legacy = save_job(tmp_path, id="f" * 20, kind="generation", dedupe_key="legacy", state="failed", phase="interrupted", progress=33)
    manager = SubtitleJobManager(tmp_path, max_cached=0, recoverable_kinds=("generation",))
    calls = []
    try:
        manager.recover_interrupted(lambda job: lambda context: calls.append(context.job_id) or {})
        wait_for(manager, legacy, lambda row: row["state"] == "succeeded")
        assert calls == [legacy]
    finally:
        manager.shutdown()


def test_recovery_drains_bounded_queue_and_can_cancel_waiting_job(tmp_path):
    identifiers = [save_job(tmp_path, id=f"{index:020x}", kind="generation", dedupe_key=str(index), state="running", created_at=str(index)) for index in range(4)]
    manager = SubtitleJobManager(tmp_path, max_pending=1, recoverable_kinds=("generation",))
    entered, release = threading.Event(), threading.Event()
    calls = []

    def run(context):
        calls.append(context.job_id)
        entered.set()
        assert release.wait(3)
        return {}

    try:
        manager.recover_interrupted(lambda job: run)
        assert entered.wait(2)
        assert len(manager._futures) == 1
        manager.cancel(identifiers[2])
        release.set()
        wait_for(manager, identifiers[-1], lambda row: row["state"] == "succeeded")
        assert calls == [identifiers[0], identifiers[1], identifiers[3]]
        assert manager.get(identifiers[2])["state"] == "canceled"
    finally:
        release.set()
        manager.shutdown()


def test_application_start_resumes_same_job_and_only_missing_real_pipeline_chunks(tmp_path, monkeypatch, application_services):
    monkeypatch.setattr(api.settings, "content_bot_data_dir", tmp_path)
    video, media, service, _, _ = pipeline_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "_uploaded_video_path", lambda _: video)
    monkeypatch.setattr(api, "probe_media_cached", lambda *a, **k: media)
    monkeypatch.setattr(application_services, "gemini_subtitle_service", service)
    manager = SubtitleJobManager(tmp_path / "generation-jobs", recoverable_kinds=("generation",))
    monkeypatch.setattr(application_services, "gemini_subtitle_jobs", manager)
    waiting = threading.Event()
    submitted = threading.Event()

    # The fake remote call cooperates with the real job cancellation event.
    def first_call(file, *a, **kwargs):
        if file.endswith("00002"):
            waiting.set()
            assert submitted.wait(2)
            assert manager._cancel_events[job["id"]].wait(5)
            from app.services.subtitle_jobs import SubtitleJobCanceled
            raise SubtitleJobCanceled("stopping")
        return json.dumps({"segments": [cue()]})

    monkeypatch.setattr(service, "_generate_content", first_call)
    job = api.generate_subtitles_with_gemini_endpoint(
        GeminiSubtitleRequest(video_id="a" * 12, regenerate=True, options={"model": "gemini-3.6-flash"}), services=application_services)
    submitted.set()
    assert waiting.wait(5), manager.get(job["id"])
    wait_for(manager, job["id"], lambda row: row.get("details", {}).get("completed") == 2)
    manager.shutdown()
    assert manager.get(job["id"])["phase"] == "interrupted"
    assert len(list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))) == 2
    saved_options = json.loads(next((tmp_path / "gemini-runs").glob("*.json")).read_text())["options"]
    calls = []
    monkeypatch.setattr(service, "_generate_content", lambda file, *a, **kw: calls.append((file, kw.get("model"))) or json.dumps({"segments": [cue()]}))
    monkeypatch.setattr(api.settings, "content_bot_gemini_model", "gemini-3.8-flash")
    restored = SubtitleJobManager(tmp_path / "generation-jobs", recoverable_kinds=("generation",))
    monkeypatch.setattr(application_services, "gemini_subtitle_jobs", restored)
    asyncio.run(application_services.start(scheduler=False))
    completed = wait_for(restored, job["id"], lambda row: row["state"] in {"failed", "succeeded"})
    assert completed["state"] == "succeeded", completed
    assert len(calls) == 1 and calls[0][0].endswith("00002")
    assert completed["result"]["document"]["run_id"] == saved_options["run_id"]
    assert completed["result"]["model"] == "gemini-3.6-flash"
    assert completed["result"]["version_id"]


@pytest.mark.parametrize("change", ["media", "policy", "missing"])
def test_incompatible_recipe_fails_without_generation_or_startup_failure(tmp_path, monkeypatch, application_services, change):
    from types import SimpleNamespace

    from app.services.gemini_subtitles import gemini_generation_cache_key

    monkeypatch.setattr(api.settings, "content_bot_data_dir", tmp_path)
    options = {"model": "gemini-3.6-flash", "pipeline_policy": {"version": 1}}
    media = {"fingerprint": "original"}
    key = gemini_generation_cache_key(media, options, model=options["model"])
    recipe = tmp_path / "gemini-runs" / f"{key}.json"
    recipe.parent.mkdir()
    if change != "missing":
        recipe.write_text(json.dumps({"version": 1, "video_id": "a" * 12, "media_fingerprint": "original", "options": options}))
    monkeypatch.setattr(api, "_uploaded_video_path", lambda _: tmp_path / "video.mp4")
    monkeypatch.setattr(api, "probe_media_cached", lambda *a, **kw: {"fingerprint": "changed"} if change == "media" else media)
    monkeypatch.setattr(application_services, "gemini_subtitle_service", SimpleNamespace(
        cache_policy=lambda: {"version": 2 if change == "policy" else 1},
        generate=lambda *a: pytest.fail("Must not generate with an incompatible recipe")))
    identifier = save_job(tmp_path / "jobs", id="a" * 20, kind="generation", dedupe_key=key, state="running")
    manager = SubtitleJobManager(tmp_path / "jobs", recoverable_kinds=("generation",))
    monkeypatch.setattr(application_services, "gemini_subtitle_jobs", manager)
    asyncio.run(application_services.start(scheduler=False))
    failed = wait_for(manager, identifier, lambda row: row["state"] == "failed")
    assert failed["phase"] == "failed" and failed["error"]
    manager.recover_interrupted(lambda _: pytest.fail("Permanent errors must not loop"))
