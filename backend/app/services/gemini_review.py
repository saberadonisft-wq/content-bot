"""User-requested media review, durable snapshots and conflict-checked proposals."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    model_validator,
)

from ..schemas import SubtitleCueV2, SubtitleDocumentV2
from .gemini_dispatch import ChunkRequestBudget, ModelFallback
from .gemini_media import atomic_json, digest_json
from .gemini_prompts import build_review_prompt
from .subtitle_readability import is_long_cue, sentence_count, validate_long_split
from .subtitle_timing_review import timing_findings, timing_regions, validate_retiming

REVIEW_VERSION = 1
REVIEW_PROMPT_VERSION = 6
COMBINED_PROMPT_VERSION = 7
_store_lock = threading.RLock()


class ReviewScope(BaseModel):
    combined: bool = False
    mode: Literal["all", "range", "selected", "long", "timing"] = "all"
    start_ms: StrictInt | None = Field(default=None, ge=0)
    end_ms: StrictInt | None = Field(default=None, ge=0)
    cue_ids: list[str] = Field(default_factory=list, max_length=20_000)
    region_ids: list[str] | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def valid_scope(self):
        if self.combined and self.mode not in {"all", "range", "selected"}:
            raise ValueError("Kiểm tra kết hợp cần chọn toàn video, khoảng thời gian hoặc nhóm cue.")
        if self.mode == "range" and (self.start_ms is None or self.end_ms is None or self.end_ms <= self.start_ms):
            raise ValueError("Phạm vi thời gian không hợp lệ.")
        if self.mode == "selected" and not self.cue_ids:
            raise ValueError("Chưa chọn cue cần kiểm tra.")
        if self.mode == "timing" and self.region_ids == []:
            raise ValueError("Chưa chọn vùng timing cần kiểm tra.")
        return self


class ProposedCue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(ge=0)
    text: str = Field(min_length=1, max_length=4000)
    source_text: str | None = Field(default=None, max_length=4000)
    source_language: str | None = Field(default=None, max_length=32)
    content_source: Literal["audio", "screen", "mixed", "unknown"] = "unknown"


class RawProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["edit", "add", "delete", "merge", "split", "retime"]
    issue: Literal["missing", "unsupported", "translation", "terminology", "duplicate", "truncated", "timing", "readability"]
    cue_ids: list[str] = Field(default_factory=list, max_length=100)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(ge=0)
    after: list[ProposedCue] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=4000)
    certainty: Literal["low", "medium", "high"] = "medium"


def review_regions(document: dict, scope: ReviewScope, duration_ms: int) -> tuple[list[tuple[int, int]], set[str]]:
    cues = document["segments"]
    known = {cue["id"] for cue in cues}
    if scope.mode == "timing":
        found = timing_regions(document, duration_ms)
        if scope.region_ids is not None:
            if not set(scope.region_ids) <= {r["id"] for r in found}:
                raise ValueError("Vùng timing đã thay đổi; hãy quét lại phụ đề hiện tại.")
            found = [r for r in found if r["id"] in scope.region_ids]
        found = [r for r in found if r["cue_ids"]]
        if not found:
            raise ValueError("Không có vùng timing với cue chưa khóa cần kiểm tra.")
        return [(r["start_ms"], r["end_ms"]) for r in found], {cue_id for r in found for cue_id in r["cue_ids"]}
    if scope.mode == "long":
        ids = [cue["id"] for cue in cues if not cue.get("locked") and is_long_cue(cue)]
        if not ids:
            raise ValueError("Không có phụ đề dài chưa khóa cần tách.")
        return review_regions(document, ReviewScope(mode="selected", cue_ids=ids), duration_ms)
    if scope.mode == "all":
        return [(0, duration_ms)], known
    if scope.mode == "range":
        if scope.end_ms > duration_ms:
            raise ValueError("Phạm vi kiểm tra vượt thời lượng video.")
        return [(scope.start_ms, scope.end_ms)], {c["id"] for c in cues if c["start_ms"] >= scope.start_ms and c["end_ms"] <= scope.end_ms}
    if not set(scope.cue_ids) <= known:
        raise ValueError("Cue được chọn không tồn tại trong snapshot.")
    intervals = sorted((cue["start_ms"], cue["end_ms"]) for cue in cues if cue["id"] in scope.cue_ids)
    regions = []
    for start, end in intervals:
        if end > duration_ms:
            raise ValueError("Cue được chọn vượt thời lượng video.")
        if regions and start <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(end, regions[-1][1]))
        else:
            regions.append((start, end))
    return regions, set(scope.cue_ids)


def _in_regions(start: int, end: int, regions) -> bool:
    return any(a <= start < end <= b for a, b in regions)


def validate_proposals(raw: str, *, snapshot: dict, regions, allowed_ids: set[str],
                       media_start_ms: int, media_end_ms: int, proposal_prefix: str, split_long: bool = False,
                       timing_only: bool = False, combined: bool = False) -> list[dict]:
    if len(raw.encode("utf-8")) > 8_000_000:
        raise ValueError("Kết quả review vượt giới hạn tài nguyên.")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("proposals"), list):
        raise TypeError("Review phải trả danh sách proposals.")
    if len(payload["proposals"]) > 500:
        raise ValueError("Một clip review vượt giới hạn 500 đề xuất.")
    by_id = {cue["id"]: cue for cue in snapshot["segments"]}
    proposals = []
    split_ids = set()
    for index, value in enumerate(payload["proposals"]):
        proposal = RawProposal.model_validate(value)
        if split_long and (proposal.operation != "split" or proposal.issue != "readability"):
            raise ValueError("Kiểm tra phụ đề dài chỉ nhận đề xuất split/readability.")
        if timing_only and (proposal.operation != "retime" or proposal.issue != "timing"):
            raise ValueError("Kiểm tra timing chỉ nhận đề xuất retime/timing, không thêm/xóa/tách lời.")
        retiming = proposal.operation == "retime"
        if not timing_only and not combined and retiming:
            raise ValueError("Thao tác retime chỉ dùng trong kiểm tra timing.")
        if retiming and proposal.issue != "timing":
            raise ValueError("Đặt lại thời gian phải dùng issue=timing.")
        ids = proposal.cue_ids
        if len(ids) != len(set(ids)) or not set(ids) <= allowed_ids:
            raise ValueError("Đề xuất tham chiếu cue ngoài phạm vi được chọn.")
        count_before, count_after = len(ids), len(proposal.after)
        valid_counts = {"edit": count_before == count_after == 1,
                        "add": count_before == 0 and count_after >= 1,
                        "delete": count_before >= 1 and count_after == 0,
                        "merge": count_before >= 2 and count_after == 1,
                        "split": count_before == 1 and count_after >= 2,
                        "retime": count_before == count_after and count_before >= 1}
        if not valid_counts[proposal.operation]:
            raise ValueError("Quan hệ cue trước/sau không đúng thao tác đề xuất.")
        start, end = proposal.start_ms + media_start_ms, proposal.end_ms + media_start_ms
        if not (media_start_ms <= start < end <= media_end_ms) or not _in_regions(start, end, regions):
            raise ValueError("Đề xuất vượt phạm vi thời gian được yêu cầu.")
        before = [deepcopy(by_id[cue_id]) for cue_id in ids]
        if any(not _in_regions(cue["start_ms"], cue["end_ms"], regions) for cue in before):
            raise ValueError("Không được sửa cue nằm ngoài phạm vi review.")
        after = []
        for position, proposed in enumerate(proposal.after):
            cue_id = ids[0] if proposal.operation in {"edit", "merge"} or (proposal.operation == "split" and position == 0) else f"rv-{proposal_prefix}-{index}-{position}"
            base = before[0] if proposal.operation in {"edit", "split"} else {}
            if retiming:
                cue_id, base = ids[position], before[position]
            updated = {**base, **proposed.model_dump(exclude_unset=True), "id": cue_id,
                       "start_ms": proposed.start_ms + media_start_ms, "end_ms": proposed.end_ms + media_start_ms,
                       "words": None, "speech_start_ms": None, "speech_end_ms": None,
                       "revision": max([cue.get("revision", 0) for cue in before] or [0]) + 1,
                       "timing_source": "gemini_estimate", "timing_precision_ms": 100,
                       "needs_review": True, "locked": False}
            if base.get("secondary_text") is not None and "source_text" in proposed.model_fields_set:
                updated["secondary_text"] = proposed.source_text
            if not (media_start_ms <= updated["start_ms"] < updated["end_ms"] <= media_end_ms) or not _in_regions(updated["start_ms"], updated["end_ms"], regions):
                raise ValueError("Cue sau sửa vượt phạm vi media/review.")
            after.append(SubtitleCueV2.model_validate(updated).model_dump(mode="json"))
        if timing_only or combined:
            if split_ids.intersection(ids):
                raise ValueError("Một cue không được xuất hiện trong nhiều đề xuất timing.")
            split_ids.update(ids)
            if retiming:
                validate_retiming(before, after)
        if combined:
            if any(cue.get("locked") for cue in before):
                raise ValueError("Không được sửa cue đã khóa.")
            if any(is_long_cue(cue) for cue in after):
                raise ValueError("Cue sau sửa vẫn quá dài hoặc chứa nhiều câu: tối đa một câu, 84 ký tự, 6 giây và 2 dòng; hãy tách theo media.")
            if proposal.operation == "split" and proposal.issue == "readability":
                validate_long_split(before[0], after, allow_retime=True)
        if split_long:
            if ids[0] in split_ids:
                raise ValueError("Một cue dài chỉ được có một đề xuất tách.")
            split_ids.add(ids[0])
            validate_long_split(before[0], after)
        all_cues = before + after
        if any(cue["start_ms"] < start or cue["end_ms"] > end for cue in all_cues):
            raise ValueError("Vùng đề xuất không bao trùm các cue trước/sau.")
        proposal_record = {**proposal.model_dump(exclude={"after"}), "id": f"proposal-{proposal_prefix}-{index}",
            "start_ms": start, "end_ms": end, "before": before, "after": after,
            "source_revision": snapshot.get("revision", 0), "state": "pending",
            "locked": any(cue.get("locked") for cue in before)}
        if retiming or combined:
            proposal_record["context_before"] = [deepcopy(c) for c in snapshot["segments"]
                                                  if c["start_ms"] < end and c["end_ms"] > start]
        proposals.append(proposal_record)
    return proposals


def validate_review_response(raw: str, *, core_start_ms: int, core_end_ms: int, **kwargs):
    """Reject each unsafe proposal atomically, retaining independent valid suggestions."""
    if len(raw.encode("utf-8")) > 8_000_000:
        raise ValueError("Kết quả review vượt giới hạn tài nguyên.")
    payload = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
    if not isinstance(payload, dict) or not isinstance(payload.get("proposals"), list):
        raise TypeError("Review phải trả danh sách proposals.")
    if len(payload["proposals"]) > 500:
        raise ValueError("Một clip review vượt giới hạn 500 đề xuất.")
    accepted, rejected, used = [], [], set()
    prefix = kwargs.pop("proposal_prefix")
    for index, value in enumerate(payload["proposals"]):
        try:
            proposal = validate_proposals(json.dumps({"proposals": [value]}),
                proposal_prefix=f"{prefix}-{index}", **kwargs)[0]
            if not core_start_ms <= proposal["start_ms"] < proposal["end_ms"] <= core_end_ms:
                raise ValueError("Đề xuất vượt phạm vi được sửa của clip.")
            if used.intersection(proposal["cue_ids"]):
                raise ValueError("Cue đã có đề xuất khác trong cùng clip.")
        except (ValueError, TypeError) as exc:
            # A mixed group is rejected in full; never remove just its unauthorized IDs.
            rejected.append({"code": "review_proposal_rejected", "message": f"Bỏ đề xuất {index + 1}: {str(exc)[:240]}",
                             "start_ms": core_start_ms, "end_ms": core_end_ms})
            continue
        used.update(proposal["cue_ids"])
        accepted.append(proposal)
    return accepted, rejected


class GeminiReviewStore:
    def __init__(self, root: Path):
        self.root = root

    def path(self, review_id):
        if not re.fullmatch(r"[a-f0-9]{32}", review_id):
            raise ValueError("Review ID không hợp lệ.")
        return self.root / f"{review_id}.json"

    def load(self, review_id):
        with _store_lock:
            record = json.loads(self.path(review_id).read_text(encoding="utf-8"))
            if record.get("version") != REVIEW_VERSION or record.get("id") != review_id:
                raise ValueError("Định dạng review không được hỗ trợ.")
            return record

    def create(self, document, scope: ReviewScope, media, model, video_id):
        snapshot = SubtitleDocumentV2.model_validate(document).model_dump(mode="json")
        regions, allowed_ids = review_regions(snapshot, scope, int(media["duration_ms"]))
        record = {"version": REVIEW_VERSION, "prompt_version": COMBINED_PROMPT_VERSION if scope.combined else REVIEW_PROMPT_VERSION,
                  "id": uuid.uuid4().hex, "video_id": video_id, "snapshot": snapshot,
                  "snapshot_hash": digest_json(snapshot), "scope": scope.model_dump(), "regions": regions,
                  "allowed_ids": sorted(allowed_ids), "media": media, "model": model,
                  "proposals": [], "clips": {}, "state": "queued", "history": [], "warnings": []}
        with _store_lock:
            atomic_json(self.path(record["id"]), record)
        return self.public(record)

    @staticmethod
    def public(record):
        return {k: deepcopy(v) for k, v in record.items() if k not in {"history", "media", "clips"}} | {"can_undo": bool(record["history"])}

    def update_run(self, review_id, *, state=None, clip_id=None, proposals=None, warnings=None):
        with _store_lock:
            record = self.load(review_id)
            if state:
                record["state"] = state
            if clip_id is not None and clip_id not in record["clips"]:
                record["proposals"].extend(proposals or [])
                record["clips"][clip_id] = True
            if warnings:
                record["warnings"].extend(warnings)
            atomic_json(self.path(review_id), record)
            return self.public(record)

    def apply(self, review_id, document, proposal_ids: list[str], *, skip=False):
        current = SubtitleDocumentV2.model_validate(document).model_dump(mode="json")
        with _store_lock:
            record = self.load(review_id)
            if current.get("run_id") != record["snapshot"].get("run_id"):
                raise ValueError("Bản phụ đề hiện tại thuộc lần tạo khác; hãy kiểm tra lại.")
            known = {proposal["id"] for proposal in record["proposals"]}
            if not proposal_ids or not set(proposal_ids) <= known or len(proposal_ids) != len(set(proposal_ids)):
                raise ValueError("Danh sách đề xuất không hợp lệ.")
            before_document = deepcopy(current)
            applied = []
            previous_states = {}
            for proposal in record["proposals"]:
                if proposal["id"] not in proposal_ids or proposal["state"] != "pending":
                    continue
                if skip:
                    proposal["state"] = "skipped"
                    continue
                current_by_id = {cue["id"]: cue for cue in current["segments"]}
                # Content, timing, revision, lock and metadata are compared, not just ID.
                conflict = any(current_by_id.get(cue["id"]) != cue for cue in proposal["before"])
                snapshot_region = [c for c in record["snapshot"]["segments"] if c["start_ms"] < proposal["end_ms"] and c["end_ms"] > proposal["start_ms"]]
                current_region = [c for c in current["segments"] if c["start_ms"] < proposal["end_ms"] and c["end_ms"] > proposal["start_ms"]]
                if proposal["operation"] == "add" and "context_before" not in proposal and snapshot_region != current_region:
                    conflict = True
                if "context_before" in proposal:
                    # Timing was inferred using neighbors too. Protect newer edits to them.
                    expected = {c["id"]: c for c in proposal.get("context_before", snapshot_region)}
                    for earlier in record["proposals"]:
                        if earlier["state"] == "applied":
                            for cue in earlier["before"]:
                                expected.pop(cue["id"], None)
                            for cue in earlier["after"]:
                                if cue["start_ms"] < proposal["end_ms"] and cue["end_ms"] > proposal["start_ms"]:
                                    expected[cue["id"]] = cue
                    if expected != {c["id"]: c for c in current_region}:
                        conflict = True
                before_ids = {cue["id"] for cue in proposal["before"]}
                if any(cue["id"] in current_by_id and cue["id"] not in before_ids for cue in proposal["after"]):
                    conflict = True
                if proposal["locked"] or any(current_by_id.get(cue_id, {}).get("locked") for cue_id in before_ids):
                    conflict = True
                if conflict:
                    proposal["state"] = "conflict"
                    continue
                previous_states[proposal["id"]] = proposal["state"]
                current["segments"] = [cue for cue in current["segments"] if cue["id"] not in before_ids] + deepcopy(proposal["after"])
                current["segments"].sort(key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]))
                proposal["state"] = "applied"
                applied.append(proposal["id"])
            if applied:
                current["revision"] += 1
                current = SubtitleDocumentV2.model_validate(current).model_dump(mode="json")
                record["history"].append({"before": before_document, "after_hash": digest_json(current),
                                          "proposal_states": previous_states})
            atomic_json(self.path(review_id), record)
            return {"document": current, "review": self.public(record), "applied_ids": applied,
                    "can_undo": bool(record["history"])}

    def undo(self, review_id, document):
        current = SubtitleDocumentV2.model_validate(document).model_dump(mode="json")
        with _store_lock:
            record = self.load(review_id)
            if not record["history"]:
                raise ValueError("Không có lần áp dụng để hoàn tác.")
            transaction = record["history"][-1]
            if digest_json(current) != transaction["after_hash"]:
                raise ValueError("Phụ đề đã được chỉnh sau khi áp dụng; không thể hoàn tác ghi đè thay đổi mới.")
            record["history"].pop()
            for proposal in record["proposals"]:
                if proposal["id"] in transaction["proposal_states"]:
                    proposal["state"] = transaction["proposal_states"][proposal["id"]]
            restored = transaction["before"]
            atomic_json(self.path(review_id), record)
            return {"document": restored, "review": self.public(record), "can_undo": bool(record["history"])}


def _review_clips(record):
    clips = []
    for start, end in record["regions"]:
        while start < end:
            core_end = min(start + 120_000, end)
            while core_end < end:
                crossing = [cue["end_ms"] for cue in record["snapshot"]["segments"] if cue["start_ms"] < core_end < cue["end_ms"]]
                if not crossing:
                    break
                core_end = min(end, max(crossing))
                if core_end - start > 600_000:
                    raise ValueError("Vùng cue chồng liên tục quá dài để review; hãy chọn khoảng thời gian nhỏ hơn.")
            padding = 10000 if record.get("scope", {}).get("combined") else 2000
            clips.append({"id": f"clip-{len(clips):05d}", "core_start_ms": start, "core_end_ms": core_end,
                          "media_start_ms": max(0, start - padding),
                          "media_end_ms": min(record["media"]["duration_ms"], core_end + padding)})
            start = core_end
    return clips


def run_review(service, store: GeminiReviewStore, review_id: str, video: Path, context):
    from .gemini_subtitles import GeminiApiError
    record = store.load(review_id)
    combined = record["scope"].get("combined", False)
    if record.get("prompt_version") != (COMBINED_PROMPT_VERSION if combined else REVIEW_PROMPT_VERSION):
        raise ValueError("Quy tắc review đã đổi; hãy tạo lượt kiểm tra mới.")
    proposal_schema = TypeAdapter(list[RawProposal]).json_schema()
    response_schema = {"type": "object", "required": ["proposals"],
                       "properties": {"proposals": {key: value for key, value in proposal_schema.items() if key != "$defs"}},
                       "$defs": proposal_schema.get("$defs", {})}
    clips = _review_clips(record)
    split_long = record["scope"]["mode"] == "long"
    timing_only = record["scope"]["mode"] == "timing"
    # Find every suspect interval before any media is sent; keep exact boundaries in the prompt.
    timing_hints = timing_findings(record["snapshot"], int(record["media"]["duration_ms"])) if combined or timing_only else []
    fallback = ModelFallback(record["model"])
    store.update_run(review_id, state="running")
    progress_lock = threading.RLock()
    stages = {}
    finished_clips = set(record["clips"])
    failed_clips = set()
    last_note = "Đang chuẩn bị vùng kiểm tra"
    started_at = time.monotonic()

    def report(clip_id=None, phase=None, note=None):
        nonlocal last_note
        with progress_lock:
            if clip_id is not None:
                if phase in {"done", "failed"}:
                    stages.pop(clip_id, None)
                    (finished_clips if phase == "done" else failed_clips).add(clip_id)
                else:
                    stages[clip_id] = phase
            if note:
                last_note = note
            elapsed = int(time.monotonic() - started_at)
            message = (f"Đã xong {len(finished_clips)}/{len(clips)} vùng · Đang xử lý {len(stages)} · Lỗi {len(failed_clips)}"
                       f" · {elapsed // 60}:{elapsed % 60:02d} — {last_note}")
            context.update(round(len(finished_clips) / max(1, len(clips)) * 95), "reviewing_media", message)

    def process_clip(index, clip):
        context.raise_if_canceled()
        def status(phase, note):
            report(clip["id"], phase, f"Vùng {index + 1}: {note}")
        status("preparing", "chuẩn bị video")
        deadline = time.monotonic() + min(service.settings.timeout_seconds, 300 if combined else service.settings.timeout_seconds)
        budget = ChunkRequestBudget(deadline, context.cancel_event, service.settings.max_retries + 1)
        service._request_context.deadline = deadline
        workspace = service.settings.job_root / f"review-{review_id}-{uuid.uuid4().hex}"
        workspace.mkdir(parents=True, exist_ok=True)
        proxy = workspace / "review.mp4"
        try:
            while not service.dispatcher.compression.acquire(timeout=0.25):
                context.raise_if_canceled()
                if time.monotonic() >= deadline:
                    raise RuntimeError("Hết thời gian chờ chuẩn bị review.")
            try:
                service._create_proxy(video, proxy, start_seconds=clip["media_start_ms"] / 1000,
                    duration_seconds=(clip["media_end_ms"] - clip["media_start_ms"]) / 1000,
                    cancel_event=context.cancel_event, deadline=deadline)
            finally:
                service.dispatcher.compression.release()
            cues = [deepcopy(cue) for cue in record["snapshot"]["segments"] if cue["start_ms"] < clip["media_end_ms"] and cue["end_ms"] > clip["media_start_ms"]]
            for cue in cues:
                cue["start_ms"] -= clip["media_start_ms"]
                cue["end_ms"] -= clip["media_start_ms"]
            hints = [{**finding,
                      "start_ms": max(clip["core_start_ms"], finding["start_ms"]) - clip["media_start_ms"],
                      "end_ms": min(clip["core_end_ms"], finding["end_ms"]) - clip["media_start_ms"],
                      "reasons": [finding["reason"]]} for finding in timing_hints
                     if finding["start_ms"] < clip["core_end_ms"] and finding["end_ms"] > clip["core_start_ms"]]
            mode = "combined" if combined else "timing" if timing_only else "long" if split_long else "general"
            scope_note = (
                f"Editable clip interval: {clip['core_start_ms'] - clip['media_start_ms']}"
                f"-{clip['core_end_ms'] - clip['media_start_ms']} ms. "
            )
            if combined or timing_only:
                targets = [cue for cue in cues if cue["id"] in record["allowed_ids"]
                           and (not combined or not cue.get("locked"))
                           and cue["start_ms"] + clip["media_start_ms"] >= clip["core_start_ms"]
                           and cue["end_ms"] + clip["media_start_ms"] <= clip["core_end_ms"]]
                target_ids = {cue["id"] for cue in targets}
                if timing_only and not target_ids:
                    return [], []
                data = {"targets": targets, "timing_hints": hints,
                        "readonly_context": [cue for cue in cues if cue["id"] not in target_ids]}
                if combined:
                    data.update(
                        long_cue_ids=[cue["id"] for cue in targets if is_long_cue(cue)],
                        readability_hints=[{"cue_id": cue["id"], "start_ms": cue["start_ms"],
                            "end_ms": cue["end_ms"], "sentence_count": sentence_count(cue["text"])}
                            for cue in targets if is_long_cue(cue)],
                    )
            elif split_long:
                targets = [cue for cue in cues if cue["id"] in record["allowed_ids"]
                           and clip["core_start_ms"] <= (cue["start_ms"] + cue["end_ms"]) / 2
                           + clip["media_start_ms"] < clip["core_end_ms"]]
                data = {"targets": targets}
            else:
                data = {"snapshot": cues, "regions_global_ms": record["regions"],
                        "allowed_ids": record["allowed_ids"]}
                scope_note += "A proposal's midpoint must lie inside the editable clip interval. "
            prompt = build_review_prompt(mode=mode, data=data, scope=scope_note)
            clip_schema = deepcopy(response_schema)
            if combined and target_ids:
                clip_schema["$defs"]["RawProposal"]["properties"]["cue_ids"]["items"]["enum"] = sorted(target_ids)
            proposals = None
            warnings = []
            for attempt in range(service.settings.max_retries + 1):
                context.raise_if_canceled()
                status("waiting_key", f"chờ key · lần {attempt + 1}")
                with service.dispatcher.lease(record["model"], cancel_event=context.cancel_event, deadline=deadline, fallback=fallback) as owner:
                    uploaded = None
                    execution = {"model": owner["leased_model"]}
                    with service._client() as client:
                        client.event_hooks.setdefault("request", []).append(budget.before_request)
                        try:
                            status("uploading", "tải video lên Gemini")
                            uploaded = service._upload_file(proxy, client=client, api_key=owner["secret"], cancel_event=context.cancel_event, managed=True)
                            status("generating", f"đang chờ {execution['model']} phân tích")
                            raw = service._generate_content(uploaded, prompt, client=client, api_key=owner["secret"],
                                cancel_event=context.cancel_event, model=record["model"], fallback=fallback,
                                model_guard=lambda selected, owner=owner: service._guard_model(owner, selected),
                                quota_scope=service.dispatcher.scope(owner), managed=True, chunk_deadline=deadline, execution=execution, response_schema=clip_schema,
                                read_timeout_seconds=120 if combined else 600,
                                status_callback=lambda note: status("retrying", note))
                            status("validating", "kiểm tra đề xuất")
                            validation_args = {"snapshot": record["snapshot"], "regions": record["regions"],
                                "allowed_ids": target_ids if timing_only or combined else set(record["allowed_ids"]), "media_start_ms": clip["media_start_ms"],
                                "media_end_ms": clip["media_end_ms"], "proposal_prefix": f"{review_id[:12]}-{index}", "split_long": split_long,
                                "timing_only": timing_only, "combined": combined}
                            if combined:
                                proposals, rejected = validate_review_response(raw, core_start_ms=clip["core_start_ms"],
                                    core_end_ms=clip["core_end_ms"], **validation_args)
                                warnings.extend(rejected)
                                if rejected:
                                    status("validating", f"giữ {len(proposals)} đề xuất hợp lệ, loại {len(rejected)} đề xuất lỗi")
                            else:
                                proposals = validate_proposals(raw, **validation_args)
                            if timing_only and any(p["start_ms"] < clip["core_start_ms"] or p["end_ms"] > clip["core_end_ms"] for p in proposals):
                                proposals = None
                                raise ValueError("Đề xuất timing vượt phạm vi được sửa của clip.")
                            proposals = [proposal for proposal in proposals if clip["core_start_ms"] <= (proposal["start_ms"] + proposal["end_ms"]) / 2 < clip["core_end_ms"]]
                            for proposal in proposals:
                                proposal["model"] = execution["model"]
                            break
                        except GeminiApiError as exc:
                            service.dispatcher.report(owner, execution["model"], exc.failure)
                            status("retrying", str(exc)[:200])
                            if attempt >= service.settings.max_retries or exc.failure.category not in {"quota", "authentication", "permission"}:
                                raise
                        except (ValueError, TypeError) as exc:
                            status("retrying", f"kết quả chưa hợp lệ: {str(exc)[:180]}")
                            if attempt >= min(1 if combined else 2, service.settings.max_retries):
                                raise
                            if split_long or timing_only or combined:
                                prompt += "\nPrevious result was invalid: " + str(exc)[:400] + " Recheck the video and correct these errors."
                        finally:
                            if uploaded:
                                try:
                                    cleaned = service._delete_file(uploaded, client=client, api_key=owner["secret"], cancel_event=threading.Event())
                                except Exception:
                                    cleaned = False
                                if not cleaned:
                                    warnings.append({"code": "review_cleanup_failed", "message": "Chưa dọn được upload review.", "key_id": owner["id"]})
            if proposals is None:
                raise RuntimeError("Review chưa có kết quả hợp lệ trong ngân sách thử lại.")
            if split_long:
                proposed_ids = {cue_id for proposal in proposals for cue_id in proposal["cue_ids"]}
                missing = [cue["id"] for cue in targets if cue["id"] not in proposed_ids]
                if missing:
                    warnings.append({"code": "long_cues_unsplit", "message": f"{len(missing)} cue dài chưa có đề xuất tách; giữ nguyên để bạn kiểm tra.", "cue_ids": missing})
            if timing_only and not proposals:
                warnings.append({"code": "timing_no_change", "message": f"Vùng {clip['core_start_ms'] / 1000:.1f}–{clip['core_end_ms'] / 1000:.1f}s chưa có đề xuất timing; giữ nguyên. Điều này không xác nhận mọi câu đã đúng."})
            if combined:
                addressed = {cue_id for proposal in proposals for cue_id in proposal["cue_ids"]}
                unresolved = [cue["id"] for cue in targets if is_long_cue(cue) and cue["id"] not in addressed]
                if unresolved:
                    warnings.append({"code": "long_cues_unsplit", "message": f"{len(unresolved)} cue dài chưa có đề xuất sửa; cần bạn đối chiếu.", "cue_ids": unresolved})
                if not proposals:
                    warnings.append({"code": "combined_no_change", "message": f"Vùng {clip['core_start_ms'] / 1000:.1f}–{clip['core_end_ms'] / 1000:.1f}s chưa có đề xuất. Cần đối chiếu nếu vẫn thấy lỗi."})
            context.raise_if_canceled()
            return proposals, warnings
        finally:
            proxy.unlink(missing_ok=True)
            workspace.rmdir()

    try:
        remaining = iter((index, clip) for index, clip in enumerate(clips) if clip["id"] not in record["clips"])
        completed = len(record["clips"])
        workers = max(1, min(len(clips), sum(key["enabled"] for key in service.runtime_keys()))) if split_long or timing_only or combined else 1
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gemini-review") as executor:
            pending = {}
            def fill():
                while len(pending) < workers and not context.cancel_event.is_set():
                    item = next(remaining, None)
                    if item is None:
                        break
                    index, clip = item
                    pending[executor.submit(process_clip, index, clip)] = clip
            fill()
            failure = None
            consecutive_failures = 0
            paused = False
            last_heartbeat = time.monotonic()
            while pending:
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    clip = pending.pop(future)
                    try:
                        proposals, warnings = future.result()
                        context.raise_if_canceled()
                        store.update_run(review_id, clip_id=clip["id"], proposals=proposals, warnings=warnings)
                        completed += 1
                        consecutive_failures = 0
                        report(clip["id"], "done", f"Vừa lưu {len(proposals)} đề xuất · {len(warnings)} cảnh báo")
                    except Exception as exc:
                        failure = exc
                        consecutive_failures += 1
                        if not context.cancel_event.is_set():
                            note = f"Vùng {clip['core_start_ms'] / 1000:.1f}–{clip['core_end_ms'] / 1000:.1f}s lỗi: {str(exc)[:200]}"
                            store.update_run(review_id, warnings=[{"code": "review_clip_failed", "message": note}])
                            report(clip["id"], "failed", note)
                        if combined and consecutive_failures >= 3:
                            paused = True
                            if not context.cancel_event.is_set():
                                report(note="3 vùng lỗi liên tiếp; dừng nhận vùng mới, đang lưu kết quả các vùng còn chạy")
                if not context.cancel_event.is_set() and (failure is None or (combined and not paused)):
                    fill()
                if time.monotonic() - last_heartbeat >= 5 and not context.cancel_event.is_set():
                    report()
                    last_heartbeat = time.monotonic()
                if context.cancel_event.is_set():
                    for future in pending:
                        future.cancel()
            if failure is not None:
                if combined:
                    raise RuntimeError(f"Đã lưu {completed}/{len(clips)} vùng; {len(failed_clips)} vùng lỗi. "
                                       "Có thể duyệt kết quả đã lưu và tiếp tục các vùng chưa xong. " + str(failure)[:240]) from failure
                raise failure
        context.raise_if_canceled()
        return store.update_run(review_id, state="succeeded")
    except Exception:
        store.update_run(review_id, state="canceled" if context.cancel_event.is_set() else "failed")
        raise
