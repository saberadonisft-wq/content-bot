import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.services.gemini_dispatch import ApiFailure
from app.services.gemini_media import ChunkPolicy, plan_chunks
from app.services.gemini_merge import decode_chunk, merge_chunks
from app.services.gemini_subtitles import (
    GeminiApiError,
    GeminiSubtitleError,
    GeminiSubtitleService,
    GeminiSubtitleSettings,
)


def cue(text="你好", start=1000, end=2000, **extra):
    return {"id": "a", "text": "Xin chào", "source_text": text, "source_language": "zh", "content_source": "audio", "start_ms": start, "end_ms": end, **extra}


def test_decode_keeps_source_without_bilingual_and_offsets_all_timestamps():
    chunk = {"chunk_id": "c1", "media_start_ms": 98_000, "media_end_ms": 122_000, "actual_offset_ms": 30}
    raw = json.dumps({"segments": [cue(words=[{"id": "w1", "text": "你好", "start_ms": 1000, "end_ms": 1800}])]})
    actual = decode_chunk(raw, chunk, model="m", bilingual=False)[0]
    assert actual["secondary_text"] is None and actual["source_text"] == "你好"
    assert (actual["start_ms"], actual["end_ms"]) == (99030, 100030)
    assert actual["words"][0]["start_ms"] == 99030
    with pytest.raises(ValueError):
        decode_chunk(json.dumps({"segments": [cue(end=25000)]}), chunk, model="m", bilingual=False)
    assert decode_chunk('{"segments":[]}', chunk, model="m", bilingual=False) == []


def merged_inputs(left, right):
    chunks = [{"chunk_id": "c1", "core_end_ms": 120_000, "media_end_ms": 122_000}, {"chunk_id": "c2", "media_start_ms": 118_000}]
    values = [{"cues": [{**c, "origin_chunk_id": "c1", "id": f"a{i}"} for i, c in enumerate(left)]}, {"cues": [{**c, "origin_chunk_id": "c2", "id": f"b{i}"} for i, c in enumerate(right)]}]
    return merge_chunks(values, {"chunks": chunks})


def test_boundary_exact_one_to_many_preserves_one_source_sequence():
    result, warnings, audit = merged_inputs([cue("你好世界", 119000, 121000)], [cue("你好", 119100, 120000), cue("世界", 120000, 121100)])
    assert "".join(c["source_text"] for c in result) == "你好世界"
    assert len(audit) == 1 and not warnings


def test_repeated_real_speech_and_ocr_over_audio_are_not_deleted():
    result, warnings, audit = merged_inputs([cue(start=118500, end=119300)], [cue(start=120000, end=120800)])
    assert len(result) == 2 and not audit
    result, warnings, audit = merged_inputs([cue(start=119000, end=121000)], [cue(start=119000, end=121000, content_source="screen")])
    assert len(result) == 2 and not audit
    assert warnings
    assert all(c["needs_review"] for c in result)


def test_script_variants_and_ambiguous_timing_remain_reviewable():
    result, _, audit = merged_inputs([cue("汉语", 119000, 121000)], [cue("漢語", 119000, 121000)])
    assert len(result) == 2 and not audit
    result, _, audit = merged_inputs([cue(start=119000, end=121000)], [cue(start=119600, end=121600)])
    assert len(result) == 2 and not audit


def pipeline_fixture(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake video")
    media = {"duration_ms": 300_000, "fingerprint": "fake", "has_audio": False}
    manifest = plan_chunks(media["duration_ms"], [], ChunkPolicy(), fingerprint="fixture")
    monkeypatch.setattr("app.services.gemini_pipeline.prepare_manifest", lambda *a, **k: manifest)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs"), keyring_provider=lambda: [
        {"id": f"key{i}", "name": f"Key {i}", "enabled": True, "secret": f"secret{i}", "project_group": f"group{i}"} for i in range(4)])
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **kw: p.write_bytes(b"proxy"))
    monkeypatch.setattr(service, "_upload_file", lambda p, **kw: "files/" + p.stem)
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: True)
    updates = []
    event = threading.Event()
    def check():
        if event.is_set():
            raise RuntimeError("canceled")
    context = SimpleNamespace(job_id="testjob", cancel_event=event, raise_if_canceled=check, update=lambda progress, phase, msg: updates.append(progress))
    return video, media, service, context, updates


def test_out_of_order_completion_and_resume_only_failed_chunk(tmp_path, monkeypatch):
    video, media, service, context, updates = pipeline_fixture(tmp_path, monkeypatch)
    calls = []
    def generate(file, *a, **kwargs):
        calls.append(file)
        if file.endswith("00002"):
            raise GeminiSubtitleError("fixture failure")
        time.sleep(0.02 if file.endswith("00001") else 0)
        return json.dumps({"segments": [cue()]})
    monkeypatch.setattr(service, "_generate_content", generate)
    with pytest.raises(GeminiSubtitleError, match="1/3"):
        service.generate(video, media, {}, context)
    assert len(list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))) == 2
    assert updates == sorted(updates)
    calls.clear()
    def success(file, *a, **kwargs):
        calls.append(file)
        return json.dumps({"segments": [cue()]})
    monkeypatch.setattr(service, "_generate_content", success)
    result = service.generate(video, media, {}, context)
    assert calls == ["files/chunk-00002"]
    assert [c["start_ms"] for c in result["document"]["segments"]] == [1000, 119000, 239000]
    for path in (tmp_path / "jobs" / "checkpoints").glob("*/*.json"):
        assert "secret0" not in path.read_text(encoding="utf-8")
    assert result["actual_models"] == ["gemini-3.6-flash"]


@pytest.mark.parametrize("failure_kind", ["overloaded", "transient", "network"])
def test_missing_chunks_recover_automatically_without_regenerating_completed_chunks(tmp_path, monkeypatch, failure_kind):
    video, media, service, context, updates = pipeline_fixture(tmp_path, monkeypatch)
    calls, waits, messages = {}, [], []
    context.update = lambda progress, phase, message: (updates.append(progress), messages.append(message))
    def generate(file, *a, **kwargs):
        calls[file] = calls.get(file, 0) + 1
        if not file.endswith("00001") and calls[file] == 1:
            if failure_kind == "network":
                raise GeminiSubtitleError("Mất kết nối sau nhiều lần thử") from httpx.ConnectError("fixture")
            raise GeminiApiError(ApiFailure(503 if failure_kind == "overloaded" else 500, "generateContent", failure_kind))
        return json.dumps({"segments": [cue()]})
    def wait_recovery(seconds, ctx, report):
        assert len(list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))) == 1
        waits.append(seconds)
        report(seconds)
    monkeypatch.setattr(service, "_generate_content", generate)
    monkeypatch.setattr("app.services.gemini_pipeline._wait_for_recovery", wait_recovery)
    result = service.generate(video, media, {}, context)
    assert result["segment_count"] == 3
    assert calls == {"files/chunk-00001": 1, "files/chunk-00002": 2, "files/chunk-00003": 2}
    assert waits == [5]
    assert any("Tự bổ sung 2 đoạn còn thiếu" in message for message in messages)
    assert updates == sorted(updates)
    service.generate(video, media, {}, context)
    assert sum(calls.values()) == 5


def test_persistent_overload_has_bounded_recovery_and_preserves_checkpoint(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    calls, waits = {}, []
    def generate(file, *a, **kwargs):
        calls[file] = calls.get(file, 0) + 1
        if file.endswith("00002"):
            raise GeminiApiError(ApiFailure(503, "generateContent", "overloaded"))
        return json.dumps({"segments": [cue()]})
    monkeypatch.setattr(service, "_generate_content", generate)
    monkeypatch.setattr("app.services.gemini_pipeline._wait_for_recovery", lambda seconds, *a: waits.append(seconds))
    with pytest.raises(GeminiSubtitleError, match="Đã tự thử lại 3 lượt"):
        service.generate(video, media, {}, context)
    assert calls == {"files/chunk-00001": 1, "files/chunk-00002": 4, "files/chunk-00003": 1}
    assert waits == [5, 10, 20]
    assert len(list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))) == 2


def test_recovery_releases_exhausted_503_fallback_and_runs_unscheduled_chunks(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    rows = service.runtime_keys()[:1]
    service._keyring_provider = lambda: rows
    calls, waits = [], []
    def send(request):
        calls.append(request.url.path.rsplit("/", 1)[-1].split(":")[0])
        if len(calls) <= 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps({"segments": [cue()]})}]}}]})
    monkeypatch.setattr(service, "_client", lambda: httpx.Client(transport=httpx.MockTransport(send), base_url="https://example.test"))
    monkeypatch.setattr("app.services.gemini_pipeline._wait_for_recovery", lambda seconds, *a: waits.append(seconds))
    result = service.generate(video, media, {"model": "gemini-3.8-flash"}, context)
    assert result["segment_count"] == 3
    assert calls == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"] + ["gemini-3.8-flash"] * 3
    assert waits == [5]


def test_cancel_during_recovery_wait_does_not_restart_missing_chunks(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    calls = []
    def generate(file, *a, **kwargs):
        calls.append(file)
        if file.endswith("00002"):
            raise GeminiApiError(ApiFailure(503, "generateContent", "overloaded"))
        return json.dumps({"segments": [cue()]})
    context.update = lambda progress, phase, message: context.cancel_event.set() if phase == "retry_wait" else None
    monkeypatch.setattr(service, "_generate_content", generate)
    with pytest.raises(RuntimeError, match="canceled"):
        service.generate(video, media, {}, context)
    assert len(calls) == 3
    assert len(list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))) == 2
    assert not list((tmp_path / "jobs").glob("run-*"))


@pytest.mark.parametrize("error", [GeminiApiError(ApiFailure(400, "generateContent", "request")),
    GeminiApiError(ApiFailure(429, "generateContent", "quota", 86400, "daily")),
    GeminiSubtitleError("Gemini từ chối nội dung theo cơ chế bảo vệ")])
def test_permanent_errors_do_not_schedule_automatic_recovery(tmp_path, monkeypatch, error):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    def generate(*args, **kwargs):
        raise error
    monkeypatch.setattr(service, "_generate_content", generate)
    monkeypatch.setattr("app.services.gemini_pipeline._wait_for_recovery", lambda *a: pytest.fail("Do not retry permanent failures"))
    with pytest.raises(GeminiSubtitleError):
        service.generate(video, media, {}, context)


def test_empty_chunk_is_cached_and_not_retried_forever(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(service, "_generate_content", lambda f, *a, **k: calls.append(f) or '{"segments":[]}')
    assert service.generate(video, media, {}, context)["segment_count"] == 0
    assert len(calls) == 3
    assert service.generate(video, media, {}, context)["segment_count"] == 0
    assert len(calls) == 3


def test_generation_prompt_upgrade_invalidates_completed_checkpoints(tmp_path, monkeypatch):
    from app.services import gemini_subtitles

    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    calls = []
    def generate(file, prompt, **kwargs):
        calls.append(prompt)
        return json.dumps({"segments": [cue()]})
    monkeypatch.setattr(service, "_generate_content", generate)
    options = {"bilingual": False, "shared_context": "Tên nhân vật: Trương Tam"}
    cache_key = gemini_subtitles.gemini_generation_cache_key(media, options, model=service.settings.model)
    policy = service.cache_policy()
    service.generate(video, media, options, context)
    assert len(calls) == 3
    assert all("MOI CUE CHI CHUA MOT CAU" in prompt and "BAT BUOC dich theo phu de goc" in prompt
               and "Trương Tam" in prompt for prompt in calls)
    service.generate(video, media, options, context)
    assert len(calls) == 3
    monkeypatch.setattr(gemini_subtitles, "PROMPT_VERSION", gemini_subtitles.PROMPT_VERSION + 1)
    assert gemini_subtitles.gemini_generation_cache_key(media, options, model=service.settings.model) != cache_key
    assert service.cache_policy() != policy
    service.generate(video, media, options, context)
    assert len(calls) == 6


def test_no_remaining_credentials_halts_queue_before_more_compression(tmp_path, monkeypatch):
    from app.services.gemini_dispatch import NoEligibleKeys
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    original_keys = service.runtime_keys()
    service._keyring_provider = lambda: original_keys[:1]
    calls = []
    def no_keys(*args, **kwargs):
        calls.append(1)
        raise NoEligibleKeys("No eligible credentials")
    monkeypatch.setattr(service, "_generate_content", no_keys)
    with pytest.raises(GeminiSubtitleError, match="3/3"):
        service.generate(video, media, {"max_concurrent": 1}, context)
    assert len(calls) == 1
    assert not context.cancel_event.is_set()


def test_pipeline_uses_all_twelve_keys_despite_legacy_four_worker_option(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    media = {**media, "duration_ms": 1_440_000}
    manifest = plan_chunks(media["duration_ms"], [], ChunkPolicy(), fingerprint="twelve-keys")
    assert len(manifest["chunks"]) == 12
    monkeypatch.setattr("app.services.gemini_pipeline.prepare_manifest", lambda *a, **k: manifest)
    service._keyring_provider = lambda: [
        {"id": f"key{i}", "name": f"Key {i}", "enabled": True, "secret": f"secret{i}"}
        for i in range(12)
    ]
    barrier = threading.Barrier(12, timeout=8)
    used = set()
    lock = threading.Lock()
    def generate(file, *args, **kwargs):
        with lock:
            used.add(kwargs["api_key"])
        barrier.wait()
        return '{"segments":[]}'
    monkeypatch.setattr(service, "_generate_content", generate)
    result = service.generate(video, media, {"max_concurrent": 4}, context)
    assert result["segment_count"] == 0
    assert len(used) == 12


def test_cancel_inflight_finishes_cleanup_without_writing_late_checkpoint(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    cleaned = []
    def generate(file, *args, **kwargs):
        started.set()
        assert release.wait(3)
        return json.dumps({"segments": [cue()]})
    monkeypatch.setattr(service, "_generate_content", generate)
    monkeypatch.setattr(service, "_delete_file", lambda file, **kw: cleaned.append((file, kw["api_key"])) or True)
    errors = []
    def run():
        try:
            service.generate(video, media, {}, context)
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(3)
    context.cancel_event.set()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors and cleaned
    assert not list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))
    assert not list((tmp_path / "jobs").glob("run-*"))


def test_long_document_roundtrip_preserves_source_and_revision():
    from app.schemas import SubtitleDocumentV2
    from app.services.subtitles import parse_subtitles_v2
    payload = {"schema_version": 2, "revision": 3, "run_id": "run-a", "segments": [
        cue(id=f"c{i}", start=i * 2000, end=i * 2000 + 1000, locked=i == 1) for i in range(650)]}
    parsed, _ = parse_subtitles_v2(json.dumps(payload))
    document = SubtitleDocumentV2.model_validate(parsed)
    assert len(document.segments) == 650
    assert document.revision == 3 and document.run_id == "run-a"
    assert document.segments[1].source_language == "zh" and document.segments[1].locked


def test_optional_alignment_missing_model_keeps_generated_document(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_generate_content", lambda *a, **k: json.dumps({"segments": [cue()]}))
    def unavailable(*args, **kwargs):
        raise OSError("local model missing")
    monkeypatch.setattr("app.services.subtitle_alignment.align_subtitle_document", unavailable)
    result = service.generate(video, media, {"alignment_mode": "all"}, context)
    assert result["segment_count"] == 3
    assert any(warning["code"] == "optional_alignment_unavailable" for warning in result["warnings"])
