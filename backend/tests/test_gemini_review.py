import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.gemini_review import (
    GeminiReviewStore,
    ReviewScope,
    _review_clips,
    review_regions,
    run_review,
    validate_proposals,
)
from app.services.gemini_subtitles import GeminiSubtitleService, GeminiSubtitleSettings


def test_review_clips_keep_crossing_cues_whole_and_preserve_scope():
    record = {"regions": [(0, 250000)], "media": {"duration_ms": 250000}, "snapshot": {"segments": [
        {"start_ms": 119000, "end_ms": 129000}, {"start_ms": 128000, "end_ms": 131000}]}}
    clips = _review_clips(record)
    assert clips[0]["core_end_ms"] == 131000
    assert clips[1]["core_start_ms"] == 131000
    assert clips[-1]["core_end_ms"] == 250000


def document():
    return {"schema_version": 2, "language": "vi", "segments": [
        {"id": "a", "start_ms": 1000, "end_ms": 2000, "text": "Sai tên", "source_text": "张三", "source_language": "zh"},
        {"id": "b", "start_ms": 2500, "end_ms": 3500, "text": "Câu hai"}]}


def raw_proposal(**changes):
    return {"operation": "edit", "issue": "terminology", "cue_ids": ["a"], "start_ms": 1000, "end_ms": 2000,
        "after": [{"start_ms": 1000, "end_ms": 2000, "text": "Trương Tam", "source_text": "张三", "source_language": "zh"}],
        "reason": "Sai tên riêng", "evidence": "Chữ trên hình là 张三", "certainty": "high", **changes}


def ready_store(tmp_path, payload=None, doc=None):
    store = GeminiReviewStore(tmp_path)
    record = store.create(doc or document(), ReviewScope(), {"duration_ms": 5000}, "gemini-3.6-flash", "a" * 12)
    proposals = validate_proposals(json.dumps({"proposals": payload or [raw_proposal()]}), snapshot=record["snapshot"],
        regions=record["regions"], allowed_ids=set(record["allowed_ids"]), media_start_ms=0, media_end_ms=5000, proposal_prefix="test")
    store.update_run(record["id"], state="succeeded", clip_id="clip-00000", proposals=proposals)
    return store, record, proposals


def test_apply_and_undo_preserve_original_and_resume_from_disk(tmp_path):
    store, record, proposals = ready_store(tmp_path)
    snapshot = deepcopy(record["snapshot"])
    result = store.apply(record["id"], snapshot, [proposals[0]["id"]])
    assert result["document"]["segments"][0]["text"] == "Trương Tam"
    assert result["document"]["revision"] == 1
    assert store.load(record["id"])["snapshot"] == snapshot
    restored = GeminiReviewStore(tmp_path).undo(record["id"], result["document"])
    assert restored["document"] == snapshot
    assert restored["review"]["proposals"][0]["state"] == "pending"


def test_user_edits_during_review_conflict_without_overwrite(tmp_path):
    store, record, proposals = ready_store(tmp_path)
    current = deepcopy(record["snapshot"])
    current["segments"][0]["text"] = "Tên người dùng đã sửa"
    result = store.apply(record["id"], current, [proposals[0]["id"]])
    assert result["document"] == current
    assert result["review"]["proposals"][0]["state"] == "conflict"
    assert not result["applied_ids"]


def test_unrelated_edits_remain_while_valid_proposal_applies(tmp_path):
    store, record, proposals = ready_store(tmp_path)
    current = deepcopy(record["snapshot"])
    current["segments"][1]["text"] = "Câu hai mới"
    result = store.apply(record["id"], current, [proposals[0]["id"]])
    assert result["document"]["segments"][1]["text"] == "Câu hai mới"
    assert result["applied_ids"]
    modified = deepcopy(result["document"])
    modified["segments"][1]["text"] = "Sửa sau áp dụng"
    with pytest.raises(ValueError, match="chỉnh sau"):
        store.undo(record["id"], modified)


def test_locked_skip_and_apply_all_conflicts(tmp_path):
    doc = document()
    doc["segments"][0]["locked"] = True
    store, record, proposals = ready_store(tmp_path, doc=doc)
    result = store.apply(record["id"], record["snapshot"], [proposals[0]["id"]])
    assert not result["applied_ids"]
    assert result["document"] == record["snapshot"]
    store, record, proposals = ready_store(tmp_path, payload=[raw_proposal(), raw_proposal()])
    result = store.apply(record["id"], record["snapshot"], [p["id"] for p in proposals])
    assert [p["state"] for p in result["review"]["proposals"]] == ["applied", "conflict"]
    store, record, proposals = ready_store(tmp_path)
    result = store.apply(record["id"], record["snapshot"], [proposals[0]["id"]], skip=True)
    assert result["review"]["proposals"][0]["state"] == "skipped"
    assert result["document"] == record["snapshot"]


@pytest.mark.parametrize("change", [
    {"cue_ids": ["not-in-snapshot"]}, {"operation": "add"}, {"start_ms": 900, "end_ms": 7000},
    {"after": [{"start_ms": 1000, "end_ms": 6000, "text": "ngoài phạm vi"}]},
    {"after": [{"start_ms": 1800, "end_ms": 1000, "text": "ngược thời gian"}]},
])
def test_reject_out_of_scope_or_invalid_model_edits(tmp_path, change):
    with pytest.raises(ValueError):
        ready_store(tmp_path, payload=[raw_proposal(**change)])


def test_selected_scope_has_no_permission_to_edit_neighbors_or_gap():
    doc = document()
    regions, ids = review_regions(doc, ReviewScope(mode="selected", cue_ids=["a"]), 5000)
    assert regions == [(1000, 2000)] and ids == {"a"}
    with pytest.raises(ValueError):
        validate_proposals(json.dumps({"proposals": [raw_proposal(cue_ids=["b"])]}), snapshot=doc, regions=regions,
            allowed_ids=ids, media_start_ms=0, media_end_ms=5000, proposal_prefix="x")


def test_real_review_transport_uses_snapshot_media_and_owner_cleanup_then_resumes(tmp_path, monkeypatch):
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path / "jobs", api_key="secret-test"))
    store = GeminiReviewStore(tmp_path / "reviews")
    record = store.create(document(), ReviewScope(), {"duration_ms": 5000}, "gemini-3.6-flash", "a" * 12)
    monkeypatch.setattr(service, "_create_proxy", lambda v, p, **kw: p.write_bytes(b"proxy"))
    monkeypatch.setattr(service, "_upload_file", lambda *a, **kw: "files/review-owner")
    called, cleaned = [], []
    def generate(file, prompt, **kwargs):
        assert file == "files/review-owner" and "Sai tên" in prompt and "张三" in prompt
        called.append(1)
        return json.dumps({"proposals": [raw_proposal()]})
    monkeypatch.setattr(service, "_generate_content", generate)
    monkeypatch.setattr(service, "_delete_file", lambda file, **kw: cleaned.append(kw["api_key"]) or True)
    context = SimpleNamespace(cancel_event=threading.Event(), raise_if_canceled=lambda: None, update=lambda *a: None)
    result = run_review(service, store, record["id"], tmp_path / "video.mp4", context)
    assert result["state"] == "succeeded" and len(result["proposals"]) == 1
    assert cleaned == ["secret-test"]
    assert "secret-test" not in store.path(record["id"]).read_text(encoding="utf-8")
    assert run_review(service, store, record["id"], tmp_path / "video.mp4", context)["state"] == "succeeded"
    assert len(called) == 1


def test_review_api_job_apply_undo_and_invalid_scope(tmp_path, monkeypatch, application_services):
    import time

    from fastapi.testclient import TestClient

    from app.api import subtitles as api
    from app.main import app
    store = GeminiReviewStore(tmp_path / "records")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(api, "_review_store", lambda: store)
    monkeypatch.setattr(api, "_uploaded_video_path", lambda video_id: video)
    monkeypatch.setattr(api, "probe_media_cached", lambda *a, **k: {"duration_ms": 5000, "fingerprint": "abc"})
    def fake_review(service, store, review_id, video, context):
        record = store.load(review_id)
        proposals = validate_proposals(json.dumps({"proposals": [raw_proposal()]}), snapshot=record["snapshot"],
            regions=record["regions"], allowed_ids=set(record["allowed_ids"]), media_start_ms=0, media_end_ms=5000, proposal_prefix="api")
        return store.update_run(review_id, state="succeeded", clip_id="clip-00000", proposals=proposals)
    monkeypatch.setattr(api, "run_review", fake_review)
    client = TestClient(app)
    response = client.post("/api/v1/subtitles/v2/review/gemini", json={"video_id": "a" * 12, "document": document()})
    assert response.status_code == 200
    review_id = response.json()["review"]["id"]
    job_id = response.json()["job"]["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/subtitles/gemini/jobs/{job_id}")
        if status.json()["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert status.status_code == 200 and status.json()["kind"] == "review"
    result = client.get(f"/api/v1/subtitles/gemini/reviews/{review_id}").json()
    applied = client.post(f"/api/v1/subtitles/gemini/reviews/{review_id}/apply", json={"document": result["snapshot"], "proposal_ids": [result["proposals"][0]["id"]]})
    assert applied.status_code == 200 and applied.json()["document"]["segments"][0]["text"] == "Trương Tam"
    restored = client.post(f"/api/v1/subtitles/gemini/reviews/{review_id}/undo", json={"document": applied.json()["document"]})
    assert restored.status_code == 200 and restored.json()["document"] == result["snapshot"]
    assert client.post("/api/v1/subtitles/v2/review/gemini", json={"video_id": "a" * 12, "document": document(), "scope": {"mode": "range", "start_ms": 9000, "end_ms": 10000}}).status_code == 422
