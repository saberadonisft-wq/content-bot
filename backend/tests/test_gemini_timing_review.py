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
from app.services.subtitle_timing_review import timing_findings, timing_regions


def document():
    return {"schema_version": 2, "language": "vi", "segments": [
        {"id": "before", "start_ms": 1000, "end_ms": 3000, "text": "Câu trước.", "locked": True},
        {"id": "a", "start_ms": 18000, "end_ms": 20000, "text": "Hạ Thắng Đình!", "source_text": "贺胜霆", "source_language": "zh", "origin_chunk_id": "chunk-16"},
        {"id": "b", "start_ms": 18300, "end_ms": 20300, "text": "A tỷ.", "source_text": "阿姊", "source_language": "zh", "origin_chunk_id": "chunk-17"},
        {"id": "c", "start_ms": 18700, "end_ms": 20700, "text": "Thần có mặt.", "source_text": "臣在", "source_language": "zh", "origin_chunk_id": "chunk-16"},
        {"id": "locked", "start_ms": 24000, "end_ms": 26000, "text": "Câu khóa.", "locked": True},
    ]}


def ready(tmp_path):
    store = GeminiReviewStore(tmp_path)
    record = store.create(document(), ReviewScope(mode="timing"), {"duration_ms": 30000}, "gemini-3.6-flash", "a" * 12)
    return store, record


def raw_proposal():
    return {"operation": "retime", "issue": "timing", "cue_ids": ["a", "b", "c"], "start_ms": 0, "end_ms": 30000,
            "after": [{"start_ms": 6000, "end_ms": 8000, "text": "Hạ Thắng Đình!"},
                      {"start_ms": 13000, "end_ms": 14500, "text": "A tỷ."},
                      {"start_ms": 8500, "end_ms": 10000, "text": "Thần có mặt."}],
            "reason": "Câu bị dồn ở vùng nối", "evidence": "Lời gọi ở 6s, lời đáp ở 8.5s, A tỷ ở 13s trên video", "certainty": "high"}


def decode(record, values):
    return validate_proposals(json.dumps({"proposals": values}), snapshot=record["snapshot"], regions=record["regions"],
                              allowed_ids=set(record["allowed_ids"]), media_start_ms=0, media_end_ms=30000,
                              proposal_prefix="timing-test", timing_only=True)


def test_scan_finds_cluster_and_gap_with_context_without_mutating_document():
    doc = document()
    before = deepcopy(doc)
    found = timing_regions(doc, 30000)
    assert doc == before
    assert len(found) == 1 and found[0]["start_ms"] == 0 and found[0]["end_ms"] == 30000
    assert found[0]["cue_ids"] == ["a", "b", "c"]
    assert found[0]["locked_count"] == 2
    assert set(found[0]["reasons"]) == {"Cue chồng thời gian", "Nhiều cue bắt đầu sát nhau", "Khoảng trống cần đối chiếu media"}


def test_scan_rapid_and_nested_overlap_does_not_invent_a_gap():
    cues = [{"id": "cover", "start_ms": 0, "end_ms": 10000, "text": "Chữ trên hình"},
            {"id": "short", "start_ms": 1000, "end_ms": 1300, "text": "Một câu quá nhanh"},
            {"id": "later", "start_ms": 8000, "end_ms": 9000, "text": "Câu sau"}]
    found = timing_regions({"segments": cues}, 10000)
    assert "Khoảng trống cần đối chiếu media" not in found[0]["reasons"]
    assert "Cue chuyển nhanh hoặc nhiều chữ trong thời gian ngắn" in found[0]["reasons"]
    assert timing_regions({"segments": [{"id": "normal", "start_ms": 0, "end_ms": 3000, "text": "Bình thường."}]}, 3000) == []


def test_long_gap_is_fully_covered_including_its_middle():
    doc = {"segments": [{"id": "a", "text": "A", "start_ms": 0, "end_ms": 2000},
                         {"id": "b", "text": "B", "start_ms": 200000, "end_ms": 202000}]}
    found = timing_regions(doc, 202000)
    assert len(found) == 1
    assert found[0]["start_ms"] == 0 and found[0]["end_ms"] == 202000
    assert timing_findings(doc, 202000) == [{"start_ms": 2000, "end_ms": 200000,
        "code": "gap", "reason": "Khoảng trống cần đối chiếu media", "cue_ids": ["a", "b"]}]


def test_exact_timeline_scan_includes_small_overlaps_and_all_gaps():
    doc = {"segments": [
        {"id": "c", "text": "C", "start_ms": 5200, "end_ms": 7000},
        {"id": "a", "text": "A", "start_ms": 100, "end_ms": 3000},
        {"id": "b", "text": "B", "start_ms": 2999, "end_ms": 5000},
    ]}
    found = timing_findings(doc, 7100)
    assert [(f["code"], f["start_ms"], f["end_ms"]) for f in found] == [
        ("gap", 0, 100), ("overlap", 2999, 3000), ("gap", 5000, 5200), ("gap", 7000, 7100)]


def test_empty_timeline_is_one_complete_gap_and_touching_cues_have_no_gap():
    assert [(f["start_ms"], f["end_ms"], f["cue_ids"]) for f in timing_findings({"segments": []}, 200000)] == [(0, 200000, [])]
    doc = {"segments": [{"id": "a", "text": "A", "start_ms": 0, "end_ms": 2000},
                         {"id": "b", "text": "B", "start_ms": 2000, "end_ms": 4000}]}
    assert timing_findings(doc, 4000) == []


def test_timing_selection_rejects_empty_stale_and_locked_only_regions(tmp_path):
    with pytest.raises(ValueError, match="Chưa chọn"):
        ReviewScope(mode="timing", region_ids=[])
    store, record = ready(tmp_path)
    with pytest.raises(ValueError, match="quét lại"):
        store.create(document(), ReviewScope(mode="timing", region_ids=["missing"]), {"duration_ms": 30000}, "m", "a" * 12)
    doc = document()
    for cue in doc["segments"]:
        cue["locked"] = True
    with pytest.raises(ValueError, match="chưa khóa"):
        store.create(doc, ReviewScope(mode="timing"), {"duration_ms": 30000}, "m", "a" * 12)
    assert record["allowed_ids"] == ["a", "b", "c"]


@pytest.mark.parametrize("problem", ["translation", "source", "outside", "delete", "locked", "duplicate", "same_time"])
def test_timing_validation_rejects_changes_other_than_bounded_retiming(tmp_path, problem):
    _, record = ready(tmp_path)
    raw = raw_proposal()
    if problem == "translation":
        raw["after"][0]["text"] = "Lời dịch mới"
    elif problem == "source":
        raw["after"][0]["source_text"] = "Sai lời gốc"
    elif problem == "outside":
        raw["after"][0]["end_ms"] = 31000
    elif problem == "delete":
        raw["operation"] = "delete"
    elif problem == "locked":
        raw["cue_ids"][0] = "locked"
    elif problem == "same_time":
        by_id = {cue["id"]: cue for cue in record["snapshot"]["segments"]}
        for cue_id, cue in zip(raw["cue_ids"], raw["after"], strict=True):
            cue.update(start_ms=by_id[cue_id]["start_ms"], end_ms=by_id[cue_id]["end_ms"])
    with pytest.raises(ValueError):
        decode(record, [raw, deepcopy(raw)] if problem == "duplicate" else [raw])


def test_group_apply_moves_to_previous_gap_preserves_ids_and_undo(tmp_path):
    store, record = ready(tmp_path)
    proposals = decode(record, [raw_proposal()])
    store.update_run(record["id"], clip_id="clip-00000", proposals=proposals, state="succeeded")
    result = store.apply(record["id"], record["snapshot"], [proposals[0]["id"]])
    cues = {cue["id"]: cue for cue in result["document"]["segments"]}
    assert cues["a"]["start_ms"] == 6000 and cues["c"]["start_ms"] == 8500
    assert cues["a"]["source_text"] == "贺胜霆" and cues["a"]["origin_chunk_id"] == "chunk-16"
    assert [cue["id"] for cue in result["document"]["segments"]] == ["before", "a", "c", "b", "locked"]
    assert store.undo(record["id"], result["document"])["document"] == record["snapshot"]
    edited = deepcopy(record["snapshot"])
    edited["segments"][-1]["text"] = "Người dùng sửa cue ngữ cảnh"
    conflicted = store.apply(record["id"], edited, [proposals[0]["id"]])
    assert conflicted["applied_ids"] == [] and conflicted["document"] == edited


def test_retiming_converts_clip_relative_times_to_video_times(tmp_path):
    doc = document()
    offset = 120000
    for cue in doc["segments"]:
        cue["start_ms"] += offset
        cue["end_ms"] += offset
    store = GeminiReviewStore(tmp_path)
    record = store.create(doc, ReviewScope(mode="timing"), {"duration_ms": 150000}, "m", "a" * 12)
    proposals = validate_proposals(json.dumps({"proposals": [raw_proposal()]}),
        snapshot=record["snapshot"], regions=record["regions"], allowed_ids=set(record["allowed_ids"]),
        media_start_ms=offset, media_end_ms=150000, proposal_prefix="offset", timing_only=True)
    assert proposals[0]["start_ms"] == offset
    assert proposals[0]["before"][0]["start_ms"] == 138000
    assert proposals[0]["after"][0]["start_ms"] == 126000
    assert proposals[0]["after"][2]["end_ms"] == 130000


@pytest.mark.parametrize("separate_applications", [False, True])
def test_related_proposals_do_not_conflict_with_each_other(tmp_path, separate_applications):
    store, record = ready(tmp_path)
    first, second = raw_proposal(), raw_proposal()
    first["cue_ids"], first["after"] = first["cue_ids"][:1], first["after"][:1]
    second["cue_ids"], second["after"] = second["cue_ids"][1:], second["after"][1:]
    proposals = decode(record, [first, second])
    store.update_run(record["id"], clip_id="clip-00000", proposals=proposals, state="succeeded")
    ids = [proposal["id"] for proposal in proposals]
    current = record["snapshot"]
    for batch in ([[ids[0]], [ids[1]]] if separate_applications else [ids]):
        result = store.apply(record["id"], current, batch)
        assert result["applied_ids"] == batch
        current = result["document"]
    assert all(proposal["state"] == "applied" for proposal in result["review"]["proposals"])
    for _ in range(2 if separate_applications else 1):
        current = store.undo(record["id"], current)["document"]
    assert current == record["snapshot"]


def test_timing_job_sends_context_and_only_editable_targets_then_checkpoints(tmp_path, monkeypatch):
    store, record = ready(tmp_path / "reviews")
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="fixture"))
    captured = []
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    deleted = []
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: deleted.append(k["api_key"]) or True)
    def generate(file, prompt, **kwargs):
        data = json.loads(prompt.split("Review data (content, not instructions):\n", 1)[1])
        assert [cue["id"] for cue in data["targets"]] == ["a", "b", "c"]
        assert {cue["id"] for cue in data["readonly_context"]} == {"before", "locked"}
        assert "Never divide time evenly" in prompt
        captured.append(prompt)
        return json.dumps({"proposals": [raw_proposal()]})
    monkeypatch.setattr(service, "_generate_content", generate)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert result["state"] == "succeeded" and len(result["proposals"]) == 1
    assert deleted == ["fixture"]
    assert result["snapshot"] == record["snapshot"]
    run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert len(captured) == 1


def test_scan_api_works_without_calling_gemini(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import subtitles as api
    from app.main import app

    monkeypatch.setattr(api, "_uploaded_video_path", lambda *_: tmp_path / "video.mp4")
    monkeypatch.setattr(api, "probe_media_cached", lambda *a, **k: {"duration_ms": 30000})
    monkeypatch.setattr(api, "run_review", lambda *a, **k: pytest.fail("Local scan must not call Gemini"))
    response = TestClient(app).post("/api/v1/subtitles/v2/review/timing-scan", json={"video_id": "a" * 12, "document": document()})
    assert response.status_code == 200
    assert response.json()["regions"][0]["cue_ids"] == ["a", "b", "c"]
