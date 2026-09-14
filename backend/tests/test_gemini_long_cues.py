import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.gemini_review import (
    GeminiReviewStore,
    ReviewScope,
    run_review,
    validate_proposals,
)
from app.services.gemini_subtitles import GeminiSubtitleService, GeminiSubtitleSettings
from app.services.subtitle_readability import is_long_cue, sentence_count


def long_document(count=1):
    return {"schema_version": 2, "language": "vi", "segments": [
        {"id": f"long-{i}", "start_ms": 1000 + i * 12000, "end_ms": 9000 + i * 12000,
         "text": "Xin chào. Chúc một ngày tốt lành.", "source_text": "你好。祝你愉快。", "secondary_text": "你好。祝你愉快。",
         "source_language": "zh", "content_source": "mixed", "origin_chunk_id": "original-chunk", "origin_model": "original-model"}
        for i in range(count)]}


def proposal(cue):
    start, end = cue["start_ms"], cue["end_ms"]
    return {"operation": "split", "issue": "readability", "cue_ids": [cue["id"]], "start_ms": start, "end_ms": end,
            "after": [{"start_ms": start, "end_ms": start + 3000, "text": "Xin chào.", "source_text": "你好。"},
                      {"start_ms": start + 3200, "end_ms": end, "text": "Chúc một ngày tốt lành.", "source_text": "祝你愉快。"}],
            "reason": "Tách hai câu", "evidence": "Nghe thấy khoảng nghỉ giữa hai câu", "certainty": "high"}


def test_long_detection_and_scope_exclude_locked_and_short_cues(tmp_path):
    assert not is_long_cue({"text": "a" * 84, "start_ms": 0, "end_ms": 6000})
    assert is_long_cue({"text": "a" * 85, "start_ms": 0, "end_ms": 6000})
    assert is_long_cue({"text": "a\nb\nc", "start_ms": 0, "end_ms": 1000})
    doc = long_document(3)
    doc["segments"][1]["locked"] = True
    doc["segments"][2]["end_ms"] = doc["segments"][2]["start_ms"] + 1000
    doc["segments"][2]["text"] = "Một câu ngắn."
    record = GeminiReviewStore(tmp_path).create(doc, ReviewScope(mode="long"), {"duration_ms": 40000}, "m", "a" * 12)
    assert record["allowed_ids"] == ["long-0"]
    assert record["regions"] == [(1000, 9000)]
    doc["segments"][0]["locked"] = True
    with pytest.raises(ValueError, match="Không có"):
        GeminiReviewStore(tmp_path).create(doc, ReviewScope(mode="long"), {"duration_ms": 40000}, "m", "a" * 12)


@pytest.mark.parametrize("text,count", [
    ("Tướng quân, chưa biết ư? Ngài mở tiệc. Mau đến!", 3),
    ("Xin chào. Tôi đây", 2),
    ("Đi?! Thật ư?!", 2),
    ('Anh nói: “Đi thôi!”', 1),
    ("你好。你在吗？我来了！", 3),
    ("Giá 3.14 và 1,5 đồng.", 1),
    ("TS. An ở TP.HCM hôm nay.", 1),
    ("Dr. Smith đến từ U.S.A. hôm qua.", 1),
    ("Tôi... vẫn đang nghĩ… thôi.", 1),
    ("...?!", 0),
])
def test_sentence_detection(text, count):
    assert sentence_count(text) == count
    assert is_long_cue({"text": text, "start_ms": 0, "end_ms": 3000}) == (count > 1)


def test_short_multi_sentence_cue_is_selected_for_gemini(tmp_path):
    doc = long_document()
    doc["segments"][0]["end_ms"] = 4000
    record = GeminiReviewStore(tmp_path).create(doc, ReviewScope(mode="long"), {"duration_ms": 4000}, "m", "a" * 12)
    assert record["allowed_ids"] == ["long-0"]


@pytest.mark.parametrize("invalid", ["lost_text", "lost_source", "overlap", "too_long", "outside", "duplicate", "delete"])
def test_long_split_rejects_loss_bad_timing_and_other_edits(tmp_path, invalid):
    store = GeminiReviewStore(tmp_path)
    record = store.create(long_document(), ReviewScope(mode="long"), {"duration_ms": 10000}, "m", "a" * 12)
    value = proposal(record["snapshot"]["segments"][0])
    if invalid == "lost_text":
        value["after"][1]["text"] = "Chúc một ngày."
    elif invalid == "lost_source":
        value["after"][1]["source_text"] = "祝你。"
    elif invalid == "overlap":
        value["after"][1]["start_ms"] = 2000
    elif invalid == "too_long":
        value["after"][1]["text"] = "x" * 85
    elif invalid == "outside":
        value["after"][1]["end_ms"] = 9500
    elif invalid == "delete":
        value["operation"] = "delete"
    values = [value, deepcopy(value)] if invalid == "duplicate" else [value]
    with pytest.raises(ValueError):
        validate_proposals(json.dumps({"proposals": values}), snapshot=record["snapshot"], regions=record["regions"],
                           allowed_ids=set(record["allowed_ids"]), media_start_ms=0, media_end_ms=10000, proposal_prefix="test", split_long=True)


def test_twelve_long_regions_use_all_keys_and_preserve_metadata_apply_undo(tmp_path, monkeypatch):
    store = GeminiReviewStore(tmp_path / "review")
    record = store.create(long_document(12), ReviewScope(mode="long"), {"duration_ms": 150000}, "gemini-3.6-flash", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs"), keyring_provider=lambda: [
        {"id": str(i), "enabled": True, "secret": f"fake-secret-{i}", "name": str(i)} for i in range(12)])
    barrier = threading.Barrier(12, timeout=5)
    used = set()
    cleaned = []
    lock = threading.Lock()
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: cleaned.append(k["api_key"]) or True)
    def generate(file, prompt, **kwargs):
        assert "không chia đều thời gian" in prompt
        targets = json.loads(prompt.split("các cue sau:\n", 1)[1])
        assert len(targets) == 1
        with lock:
            used.add(kwargs["api_key"])
        barrier.wait()
        return json.dumps({"proposals": [proposal(targets[0])]})
    monkeypatch.setattr(service, "_generate_content", generate)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert len(result["proposals"]) == len(used) == len(cleaned) == 12
    assert all(p["after"][1]["origin_chunk_id"] == "original-chunk" for p in result["proposals"])
    assert all(p["after"][1]["secondary_text"] == "祝你愉快。" for p in result["proposals"])
    assert all(p["after"][1]["source_language"] == "zh" for p in result["proposals"])
    assert result["snapshot"] == record["snapshot"]
    applied = store.apply(record["id"], result["snapshot"], [p["id"] for p in result["proposals"]])
    assert len(applied["document"]["segments"]) == 24
    assert not any(is_long_cue(c) for c in applied["document"]["segments"])
    restored = store.undo(record["id"], applied["document"])
    assert restored["document"] == record["snapshot"]
    # Resume uses checkpoints and does not repeat any generation.
    assert run_review(service, store, record["id"], tmp_path / "video.mp4", context)["state"] == "succeeded"


def test_empty_long_review_reports_unsplit_cues(tmp_path, monkeypatch):
    store = GeminiReviewStore(tmp_path / "review")
    record = store.create(long_document(), ReviewScope(mode="long"), {"duration_ms": 10000}, "gemini-3.6-flash", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="fixture"))
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: True)
    monkeypatch.setattr(service, "_generate_content", lambda *a, **k: '{"proposals":[]}')
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert result["warnings"][0]["code"] == "long_cues_unsplit"
    assert result["snapshot"] == record["snapshot"]


def test_parallel_long_review_keeps_successful_checkpoints_when_another_region_fails(tmp_path, monkeypatch):
    store = GeminiReviewStore(tmp_path / "review")
    record = store.create(long_document(2), ReviewScope(mode="long"), {"duration_ms": 25000}, "gemini-3.6-flash", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", max_retries=0), keyring_provider=lambda: [
        {"id": str(i), "enabled": True, "secret": f"fake-{i}", "name": str(i)} for i in range(2)])
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    cleaned = []
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: cleaned.append(1) or True)
    barrier = threading.Barrier(2, timeout=5)
    def generate(file, prompt, **kwargs):
        cue = json.loads(prompt.split("các cue sau:\n", 1)[1])[0]
        barrier.wait()
        value = proposal(cue)
        if cue["id"] == "long-0":
            value["after"][0]["text"] = "Bị mất nội dung."
        return json.dumps({"proposals": [value]})
    monkeypatch.setattr(service, "_generate_content", generate)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    with pytest.raises(ValueError, match="giữ đầy đủ"):
        run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    saved = store.load(record["id"])
    assert saved["state"] == "failed"
    assert list(saved["clips"]) == ["clip-00001"]
    assert len(cleaned) == 2
    resumed = []
    def succeed(file, prompt, **kwargs):
        cue = json.loads(prompt.split("các cue sau:\n", 1)[1])[0]
        resumed.append(cue["id"])
        return json.dumps({"proposals": [proposal(cue)]})
    monkeypatch.setattr(service, "_generate_content", succeed)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert resumed == ["long-0"]
    assert result["state"] == "succeeded" and len(result["proposals"]) == 2
