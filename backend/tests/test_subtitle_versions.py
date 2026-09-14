import threading
import time
from types import SimpleNamespace

from app.api import subtitles as api
from app.schemas import GeminiSubtitleRequest
from app.services.gemini_subtitles import gemini_generation_cache_key
from app.services.subtitle_jobs import SubtitleJobManager
from app.services.subtitle_versions import SubtitleVersionStore, regeneration_options


def document(text="Bản cũ"):
    return {"schema_version": 2, "segments": [{"id": "a", "start_ms": 0, "end_ms": 1000, "text": text}]}


def test_versions_are_immutable_deduplicated_and_paged(tmp_path):
    store = SubtitleVersionStore(tmp_path)
    first = store.save("a" * 12, document())
    assert store.save("a" * 12, document())["id"] == first["id"]
    second = store.save("a" * 12, document("Bản mới"))
    assert first["id"] != second["id"]
    assert store.load("a" * 12, first["id"])["document"]["segments"][0]["text"] == "Bản cũ"
    assert store.list("a" * 12, limit=1)["total"] == 2
    assert len(store.list("a" * 12, offset=1)["versions"]) == 1


def test_regeneration_bypasses_content_cache_without_changing_media_identity():
    media = {"fingerprint": "abc", "audio_hash": "def"}
    first, second = regeneration_options({"bilingual": True}), regeneration_options({"bilingual": True})
    assert gemini_generation_cache_key(media, first, model="m") != gemini_generation_cache_key(media, second, model="m")
    assert first["run_id"] != second["run_id"]


def test_real_cache_policy_roundtrips_through_resume_json(tmp_path):
    import json

    from app.services.gemini_subtitles import (
        GeminiSubtitleService,
        GeminiSubtitleSettings,
    )
    policy = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path)).cache_policy()
    assert json.loads(json.dumps(policy)) == policy


def test_regenerate_endpoint_runs_again_and_keeps_editor_snapshot(tmp_path, monkeypatch, application_services):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    manager = SubtitleJobManager(tmp_path / "jobs")
    store = SubtitleVersionStore(tmp_path / "versions")
    monkeypatch.setattr(application_services, "gemini_subtitle_jobs", manager)
    monkeypatch.setattr(api, "_version_store", lambda: store)
    monkeypatch.setattr(api, "_uploaded_video_path", lambda *a: video)
    monkeypatch.setattr(api, "probe_media_cached", lambda *a, **k: {"fingerprint": "abc", "duration_ms": 5000, "has_audio": True})
    calls = []
    lock = threading.Lock()
    def generate(video, media, options, context):
        with lock:
            calls.append(options["run_id"])
        return {"document": {**document("Mới"), "run_id": options["run_id"]}, "model": "gemini-3.6-flash"}
    monkeypatch.setattr(application_services, "gemini_subtitle_service", SimpleNamespace(generate=generate, cache_policy=lambda: {"version": 1}))
    request = GeminiSubtitleRequest(video_id="a" * 12, regenerate=True, current_document=document())
    first = api.generate_subtitles_with_gemini_endpoint(request, services=application_services)
    second = api.generate_subtitles_with_gemini_endpoint(request, services=application_services)
    assert first["id"] != second["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and manager.get(second["id"])["state"] in {"queued", "running"}:
        time.sleep(0.01)
    assert manager.get(second["id"])["state"] == "succeeded"
    assert len(calls) == 2 and calls[0] != calls[1]
    assert store.list("a" * 12)["total"] == 3
    assert any(store.load("a" * 12, v["id"])["document"]["segments"][0]["text"] == "Bản cũ" for v in store.list("a" * 12)["versions"])
    manager.shutdown()


def test_resume_generation_preserves_run_options_after_manager_restart(tmp_path, monkeypatch, application_services):
    monkeypatch.setattr(api.settings, "content_bot_data_dir", tmp_path)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(api, "_uploaded_video_path", lambda *a: video)
    monkeypatch.setattr(api, "probe_media_cached", lambda *a, **k: {"fingerprint": "same-media", "duration_ms": 5000})
    calls = []
    def generate(video, media, options, context):
        calls.append(options)
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        return {"document": {**document("Recovered"), "run_id": options['run_id']}, "model": options['model']}
    monkeypatch.setattr(application_services, "gemini_subtitle_service", SimpleNamespace(generate=generate, cache_policy=lambda: {"version": 1}))
    manager = SubtitleJobManager(tmp_path / "jobs")
    monkeypatch.setattr(application_services, "gemini_subtitle_jobs", manager)
    first = api.generate_subtitles_with_gemini_endpoint(GeminiSubtitleRequest(video_id='a' * 12, regenerate=True), services=application_services)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and manager.get(first['id'])['state'] in {'queued', 'running'}:
        time.sleep(0.01)
    manager.shutdown()
    assert manager.get(first['id'])['state'] == 'failed'
    recovered = SubtitleJobManager(tmp_path / "jobs")
    monkeypatch.setattr(application_services, "gemini_subtitle_jobs", recovered)
    second = api.resume_gemini_generation(first['id'], services=application_services)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and recovered.get(second['id'])['state'] in {'queued', 'running'}:
        time.sleep(0.01)
    assert recovered.get(second['id'])['state'] == 'succeeded'
    assert calls[0] == calls[1]
    assert first['id'] != second['id']
    recovered.shutdown()
