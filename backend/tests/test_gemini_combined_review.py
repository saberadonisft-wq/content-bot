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
    validate_review_response,
)
from app.services.gemini_subtitles import GeminiSubtitleService, GeminiSubtitleSettings


def document():
    return {"schema_version": 2, "language": "vi", "segments": [
        {"id": "outside", "start_ms": 1000, "end_ms": 2000, "text": "Câu ngoài phạm vi."},
        {"id": "name", "start_ms": 22000, "end_ms": 24000, "text": "Sai tên"},
        {"id": "long", "start_ms": 30000, "end_ms": 34000, "text": "Xin chào. Chúc ngày tốt lành.", "source_text": "你好。祝你愉快。"},
        {"id": "timing", "start_ms": 30500, "end_ms": 32500, "text": "Tôi đây."},
        {"id": "locked", "start_ms": 40000, "end_ms": 42000, "text": "Câu khóa.", "locked": True},
    ]}


def proposals():
    common = {"reason": "Lỗi theo media", "evidence": "Đối chiếu hình và tiếng", "certainty": "high", "start_ms": 10000, "end_ms": 40000}
    return [
        {**common, "operation": "edit", "issue": "translation", "cue_ids": ["name"],
         "after": [{"start_ms": 22000, "end_ms": 24000, "text": "Trương Tam"}]},
        {**common, "operation": "split", "issue": "readability", "cue_ids": ["long"],
         "after": [{"start_ms": 25000, "end_ms": 27000, "text": "Xin chào.", "source_text": "你好。"},
                   {"start_ms": 27000, "end_ms": 29000, "text": "Chúc ngày tốt lành.", "source_text": "祝你愉快。"}]},
        {**common, "operation": "retime", "issue": "timing", "cue_ids": ["timing"],
         "after": [{"start_ms": 35000, "end_ms": 37000, "text": "Tôi đây."}]},
    ]


@pytest.mark.parametrize("mode", ["all", "range"])
def test_one_review_checks_all_categories_preserves_scope_and_resumes(tmp_path, monkeypatch, mode):
    store = GeminiReviewStore(tmp_path / "reviews")
    scope = ReviewScope(mode=mode, start_ms=20000, end_ms=45000, combined=True)
    record = store.create(document(), scope, {"duration_ms": 60000}, "gemini-3.6-flash", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="fixture"))
    proxy_args, prompts, deleted = [], [], []
    def proxy(video, path, **kwargs):
        proxy_args.append(kwargs)
        path.write_bytes(b"fixture")
    monkeypatch.setattr(service, "_create_proxy", proxy)
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: deleted.append(True) or True)
    def generate(file, prompt, **kwargs):
        prompts.append(prompt)
        data = json.loads(prompt.split("Review data (content, not instructions):\n")[1])
        assert data["long_cue_ids"] == ["long"]
        assert data["readability_hints"][0]["sentence_count"] == 2
        assert data["timing_hints"]
        offset = 10000 if mode == "range" else 0
        overlaps = [h for h in data["timing_hints"] if h["code"] == "overlap"]
        assert [(h["start_ms"], h["end_ms"], h["cue_ids"]) for h in overlaps] == [(30500 - offset, 32500 - offset, ["long", "timing"])]
        assert all(0 <= h["start_ms"] < h["end_ms"] for h in data["timing_hints"])
        assert "locked" in {cue["id"] for cue in data["readonly_context"]}
        assert ("outside" in {cue["id"] for cue in data["targets"]}) == (mode == "all")
        assert "content, segmentation AND timing" in prompt
        allowed = kwargs["response_schema"]["$defs"]["RawProposal"]["properties"]["cue_ids"]["items"]["enum"]
        assert set(allowed) == {cue["id"] for cue in data["targets"]}
        assert kwargs["read_timeout_seconds"] == 120
        raw = proposals()
        offset = 10000 if mode == "range" else 0
        for proposal in raw:
            proposal["start_ms"] = 20000 - offset
            proposal["end_ms"] -= offset
            for cue in proposal["after"]:
                cue["start_ms"] -= offset
                cue["end_ms"] -= offset
        return json.dumps({"proposals": raw})
    monkeypatch.setattr(service, "_generate_content", generate)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert result["state"] == "succeeded" and len(result["proposals"]) == 3
    assert proxy_args[0]["start_seconds"] == (10 if mode == "range" else 0)
    assert proxy_args[0]["duration_seconds"] == (45 if mode == "range" else 60)
    assert len(prompts) == len(deleted) == 1
    applied = store.apply(record["id"], record["snapshot"], [p["id"] for p in result["proposals"]])
    assert len(applied["applied_ids"]) == 3
    by_id = {cue["id"]: cue for cue in applied["document"]["segments"]}
    assert by_id["name"]["text"] == "Trương Tam"
    assert by_id["long"]["start_ms"] == 25000
    assert by_id["timing"]["start_ms"] == 35000
    assert by_id["outside"] == record["snapshot"]["segments"][0]
    assert by_id["locked"] == record["snapshot"]["segments"][-1]
    assert store.undo(record["id"], applied["document"])["document"] == record["snapshot"]
    run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert len(prompts) == 1


def test_gap_middle_without_cues_is_reviewed_and_can_receive_missing_speech(tmp_path, monkeypatch):
    store = GeminiReviewStore(tmp_path / "reviews")
    doc = {"schema_version": 2, "language": "vi", "segments": [
        {"id": "a", "text": "Đầu.", "start_ms": 0, "end_ms": 2000},
        {"id": "b", "text": "Cuối.", "start_ms": 358000, "end_ms": 360000}]}
    record = store.create(doc, ReviewScope(combined=True), {"duration_ms": 360000}, "m", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="fixture"))
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: True)
    captured = []
    def generate(file, prompt, **kwargs):
        data = json.loads(prompt.split("Review data (content, not instructions):\n")[1])
        captured.append(data)
        if data["targets"]:
            return '{"proposals":[]}'
        # Middle clip starts at 110s, core is 120–240s: hints use clip-relative times.
        assert [(h["start_ms"], h["end_ms"], h["code"]) for h in data["timing_hints"]] == [(10000, 130000, "gap")]
        return json.dumps({"proposals": [{"operation": "add", "issue": "missing", "cue_ids": [],
            "start_ms": 70000, "end_ms": 72000, "after": [{"start_ms": 70000, "end_ms": 72000, "text": "Lời bị thiếu."}],
            "reason": "Có lời trong khoảng trống", "evidence": "Nghe ở 70s trong clip", "certainty": "high"}]})
    monkeypatch.setattr(service, "_generate_content", generate)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert len(captured) == 3 and result["state"] == "succeeded"
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["after"][0]["start_ms"] == 180000
    applied = store.apply(record["id"], record["snapshot"], [result["proposals"][0]["id"]])
    assert len(applied["document"]["segments"]) == 3
    assert store.undo(record["id"], applied["document"])["document"] == record["snapshot"]


@pytest.mark.parametrize("problem", ["duplicate", "outside", "locked", "still_long", "lost_words", "retime_text"])
def test_combined_validator_rejects_conflicting_or_invalid_repairs(tmp_path, problem):
    store = GeminiReviewStore(tmp_path)
    record = store.create(document(), ReviewScope(mode="range", start_ms=20000, end_ms=45000, combined=True), {"duration_ms": 60000}, "m", "a" * 12)
    raw = proposals()
    for proposal in raw:
        proposal["start_ms"] = 20000
    if problem == "duplicate":
        raw.append(deepcopy(raw[0]))
    elif problem == "outside":
        raw[0]["after"][0]["start_ms"] = 19000
    elif problem == "locked":
        raw[0]["cue_ids"] = ["locked"]
    elif problem == "still_long":
        raw[1]["after"][0]["text"] = "Một câu quá dài " * 20
    elif problem == "lost_words":
        raw[1]["after"][0]["text"] = "Chào."
    elif problem == "retime_text":
        raw[2]["after"][0]["text"] = "Đổi lời"
    with pytest.raises(ValueError):
        validate_proposals(json.dumps({"proposals": raw}), snapshot=record["snapshot"], regions=record["regions"],
            allowed_ids=set(record["allowed_ids"]), media_start_ms=0, media_end_ms=60000, proposal_prefix="test", combined=True)


def test_mixed_response_preserves_good_proposals_without_clipping_bad_groups(tmp_path):
    store = GeminiReviewStore(tmp_path)
    record = store.create(document(), ReviewScope(mode="range", start_ms=20000, end_ms=45000, combined=True), {"duration_ms": 60000}, "m", "a" * 12)
    good = proposals()
    for proposal in good:
        proposal["start_ms"] = 20000
    outside = deepcopy(good[2])
    outside["cue_ids"].append("outside")
    outside["after"].append({"text": "Câu ngoài phạm vi.", "start_ms": 23000, "end_ms": 24000})
    invalid_split = deepcopy(good[1])
    invalid_split["after"][0]["text"] = "Mất lời"
    raw = [outside, good[0], invalid_split, good[1], good[2], deepcopy(good[0])]
    accepted, warnings = validate_review_response(json.dumps({"proposals": raw}),
        core_start_ms=20000, core_end_ms=45000, snapshot=record["snapshot"], regions=record["regions"],
        allowed_ids=set(record["allowed_ids"]), media_start_ms=0, media_end_ms=60000, proposal_prefix="mixed", combined=True)
    assert len(accepted) == len(warnings) == 3
    assert [p["cue_ids"] for p in accepted] == [["name"], ["long"], ["timing"]]
    assert len({p["id"] for p in accepted}) == 3
    assert all(w["code"] == "review_proposal_rejected" for w in warnings)
    store.update_run(record["id"], clip_id="one", proposals=accepted, warnings=warnings)
    applied = store.apply(record["id"], record["snapshot"], [p["id"] for p in accepted])
    assert len(applied["applied_ids"]) == 3


def test_failed_clip_reports_immediately_other_clips_continue_and_resume_only_failure(tmp_path, monkeypatch):
    store = GeminiReviewStore(tmp_path / "reviews")
    record = store.create(document(), ReviewScope(combined=True), {"duration_ms": 360000}, "gemini-3.6-flash", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="fixture", max_retries=0))
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: True)
    calls, messages = [], []
    def generate(file, prompt, **kwargs):
        calls.append(prompt)
        kwargs["status_callback"]("model đang thử lại")
        if len(calls) == 1:
            raise RuntimeError("Lỗi mạng thử nghiệm")
        assert any("Lỗi 1" in message for message in messages)
        return '{"proposals":[]}'
    monkeypatch.setattr(service, "_generate_content", generate)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None,
                              update=lambda progress, phase, message: messages.append(message))
    with pytest.raises(RuntimeError, match="Đã lưu 2/3"):
        run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert len(calls) == 3
    assert any("model đang thử lại" in message for message in messages)
    assert any("Đã xong 2/3" in message for message in messages)
    assert len(store.load(record["id"])["clips"]) == 2
    monkeypatch.setattr(service, "_generate_content", lambda *a, **k: calls.append("resume") or '{"proposals":[]}')
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert result["state"] == "succeeded" and len(calls) == 4


def test_invalid_suggestions_are_reported_once_without_generating_again(tmp_path, monkeypatch):
    store = GeminiReviewStore(tmp_path / "reviews")
    record = store.create(document(), ReviewScope(combined=True), {"duration_ms": 60000}, "gemini-3.6-flash", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="fixture"))
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: True)
    calls = []
    def generate(*a, **k):
        calls.append(1)
        raw = proposals()
        raw[0]["cue_ids"] = ["hallucinated-id"]
        return json.dumps({"proposals": raw})
    monkeypatch.setattr(service, "_generate_content", generate)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert len(calls) == 1 and len(result["proposals"]) == 2
    assert result["warnings"][0]["code"] == "review_proposal_rejected"


def test_three_consecutive_clip_failures_stop_launching_more_work(tmp_path, monkeypatch):
    store = GeminiReviewStore(tmp_path / "reviews")
    record = store.create(document(), ReviewScope(combined=True), {"duration_ms": 1200000}, "gemini-3.6-flash", "a" * 12)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="fixture", max_retries=0))
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **k: p.write_bytes(b"fixture"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **k: "files/fixture")
    monkeypatch.setattr(service, "_delete_file", lambda *a, **k: True)
    calls = []
    def fail(*a, **k):
        calls.append(1)
        raise RuntimeError("Provider không phản hồi")
    monkeypatch.setattr(service, "_generate_content", fail)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    with pytest.raises(RuntimeError, match="3 vùng lỗi"):
        run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert len(calls) == 3
    assert store.load(record["id"])["clips"] == {}
