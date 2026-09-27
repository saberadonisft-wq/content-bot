import json

import pytest
from test_gemini_pipeline import cue, pipeline_fixture

from app.services.gemini_merge import decode_chunk
from app.services.gemini_quality import (
    cached_quality_valid,
    evaluate_quality,
    quality_stamp,
)
from app.services.gemini_subtitles import GeminiSubtitleError

CHUNK = {"chunk_id": "c1", "media_start_ms": 948938, "media_end_ms": 1081782}


def report(*, start=73000, end=75000, indices=None, source="大夏最隐秘的尖刀部队", ok=True):
    return {"reviewed_entire_clip": True, "issues": [], "units": [{
        "start_ms": start, "end_ms": end, "source_text": source,
        "candidate_indices": [0] if indices is None else indices,
        "translation_ok": ok, "evidence": "Observed source text at the specified video interval",
    }]}


def test_rejects_in_bounds_38_second_drift_and_accepts_correct_local_times():
    raw = json.dumps({"segments": [cue("大夏最隐秘的尖刀部队", 111000, 113000)]})
    cues = decode_chunk(raw, CHUNK, model="m", bilingual=False)
    _, errors = evaluate_quality(json.dumps(report()), cues, CHUNK)
    assert any("111000-113000" in error and "73000-75000" in error for error in errors)
    corrected = json.dumps({"segments": [cue("大夏最隐秘的尖刀部队", 73000, 75000)]})
    cues = decode_chunk(corrected, CHUNK, model="m", bilingual=False)
    assert evaluate_quality(json.dumps(report()), cues, CHUNK)[1] == []


def test_rejects_merged_displays_missing_units_and_unsupported_candidates():
    raw = json.dumps({"segments": [cue("你好再见", 1000, 4000)]})
    cues = decode_chunk(raw, CHUNK, model="m", bilingual=False)
    audit = report(start=1000, end=2000, source="你好")
    audit["units"] += report(start=2000, end=4000, source="再见")["units"]
    assert any("matched 2 source displays" in error for error in evaluate_quality(json.dumps(audit), cues, CHUNK)[1])
    audit["units"][1]["candidate_indices"] = []
    assert any("missing/split/duplicate" in error for error in evaluate_quality(json.dumps(audit), cues, CHUNK)[1])
    audit["units"] = []
    assert any("matched 0 source displays" in error for error in evaluate_quality(json.dumps(audit), cues, CHUNK)[1])


def test_empty_silent_clip_requires_a_complete_audit_and_no_observed_content():
    empty = {"reviewed_entire_clip": True, "units": [], "issues": []}
    assert evaluate_quality(json.dumps(empty), [], CHUNK)[1] == []
    assert evaluate_quality(json.dumps(report(indices=[])), [], CHUNK)[1]
    empty["reviewed_entire_clip"] = False
    assert evaluate_quality(json.dumps(empty), [], CHUNK)[1]


def test_cache_requires_audit_bound_to_exact_candidate_and_rechecks_evidence():
    raw = json.dumps({"segments": [cue("大夏最隐秘的尖刀部队", 73000, 75000)]})
    cues = decode_chunk(raw, CHUNK, model="m", bilingual=False)
    cached = {"raw": raw, "quality": quality_stamp(raw, report(), model="checker")}
    assert cached_quality_valid(cached, cues, CHUNK)
    assert not cached_quality_valid({"raw": raw}, cues, CHUNK)
    cached["raw"] = raw + " "
    assert not cached_quality_valid(cached, cues, CHUNK)
    cached["raw"] = raw
    cached["quality"]["report"]["units"][0]["translation_ok"] = False
    assert not cached_quality_valid(cached, cues, CHUNK)


def test_pipeline_reports_failure_repairs_only_bad_chunk_and_rechecks_it(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    calls, audits, phases = {}, {}, []
    context.update = lambda progress, phase, message: phases.append((phase, message))

    def generate(file, prompt, **kwargs):
        calls[file] = calls.get(file, 0) + 1
        start = 39000 if file.endswith("00002") and calls[file] == 1 else 1000
        if calls[file] == 2:
            assert "REPAIR THE PREVIOUS REJECTED RESULT" in prompt
            assert "39000-40000" in prompt
        return json.dumps({"segments": [cue(start=start, end=start + 1000)]})

    def audit(file, prompt, **kwargs):
        audits[file] = audits.get(file, 0) + 1
        return json.dumps(report(start=1000, end=2000, source="你好"))

    monkeypatch.setattr(service, "_generate_content", generate)
    monkeypatch.setattr(service, "_audit_content", audit)
    result = service.generate(video, media, {}, context)
    assert calls == audits == {"files/chunk-00001": 1, "files/chunk-00002": 2, "files/chunk-00003": 1}
    assert any(phase == "quality_failed" and "Tự chạy lại ngay" in message for phase, message in phases)
    assert [c["start_ms"] for c in result["document"]["segments"]] == [1000, 119000, 239000]
    service.generate(video, media, {}, context)
    assert sum(calls.values()) == sum(audits.values()) == 4


def test_quality_failures_are_bounded_not_cached_or_recovered_as_network_errors(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    calls = {}
    def generate(file, *args, **kwargs):
        calls[file] = calls.get(file, 0) + 1
        start = 39000 if file.endswith("00002") else 1000
        return json.dumps({"segments": [cue(start=start, end=start + 1000)]})
    monkeypatch.setattr(service, "_generate_content", generate)
    monkeypatch.setattr(service, "_audit_content", lambda *a, **k: json.dumps(report(start=1000, end=2000, source="你好")))
    monkeypatch.setattr("app.services.gemini_pipeline._wait_for_recovery", lambda *a: pytest.fail("Do not reset quality limits"))
    with pytest.raises(GeminiSubtitleError, match="1/3"):
        service.generate(video, media, {}, context)
    assert calls["files/chunk-00002"] == 3
    assert len(list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))) == 2
    assert not list((tmp_path / "jobs").glob("run-*"))


def test_cancel_after_audit_does_not_accept_or_retry_candidate(tmp_path, monkeypatch):
    video, media, service, context, _ = pipeline_fixture(tmp_path, monkeypatch)
    service._keyring_provider = lambda: [{"id": "key", "name": "Key", "enabled": True,
                                         "secret": "fixture", "project_group": "fixture"}]
    monkeypatch.setattr(service, "_generate_content", lambda *a, **k: json.dumps({"segments": [cue()]}))
    def audit(*a, **k):
        context.cancel_event.set()
        return json.dumps(report(start=1000, end=2000, source="你好"))
    monkeypatch.setattr(service, "_audit_content", audit)
    with pytest.raises(RuntimeError, match="canceled"):
        service.generate(video, media, {}, context)
    assert not list((tmp_path / "jobs" / "checkpoints").glob("*/chunk-*.json"))
