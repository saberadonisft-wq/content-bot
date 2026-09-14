"""Candidate timing regions; subtitle-only heuristics never prove missing speech."""
from __future__ import annotations

from collections import deque

from .subtitle_readability import normalized_text

CONTEXT_MS = 10_000
REASONS = {
    "overlap": "Cue chồng thời gian",
    "rapid": "Cue chuyển nhanh hoặc nhiều chữ trong thời gian ngắn",
    "burst": "Nhiều cue bắt đầu sát nhau",
    "gap": "Khoảng trống cần đối chiếu media",
}


def timing_findings(document: dict, duration_ms: int) -> list[dict]:
    """Scan the union of cue intervals, including every uncovered millisecond."""
    cues = sorted(document["segments"], key=lambda c: (c["start_ms"], c["end_ms"], c["id"]))
    findings = []

    def add(start, end, code, cue_ids):
        findings.append({"start_ms": start, "end_ms": end, "code": code,
                         "reason": REASONS[code], "cue_ids": cue_ids})

    previous = None
    recent = deque()
    for cue in cues:
        start, end = cue["start_ms"], cue["end_ms"]
        if not 0 <= start < end <= duration_ms:
            raise ValueError("Cue nằm ngoài thời lượng video; sửa giới hạn media trước khi kiểm tra timing.")
        text_length = len(normalized_text(cue["text"]))
        if end - start < 700 or (text_length >= 12 and text_length * 1000 > 25 * (end - start)):
            add(start, end, "rapid", [cue["id"]])
        if previous:
            if previous["end_ms"] > start:
                add(start, min(previous["end_ms"], end), "overlap", [previous["id"], cue["id"]])
            gap = start - previous["end_ms"]
            if gap > 0:
                add(previous["end_ms"], start, "gap", [previous["id"], cue["id"]])
        elif start > 0:
            add(0, start, "gap", [cue["id"]])
        while recent and start - recent[0]["start_ms"] > 1500:
            recent.popleft()
        recent.append(cue)
        if len(recent) >= 3:
            add(recent[0]["start_ms"], max(c["end_ms"] for c in recent), "burst", [c["id"] for c in recent])
        if previous is None or end > previous["end_ms"]:
            previous = cue
    if previous and duration_ms > previous["end_ms"]:
        add(previous["end_ms"], duration_ms, "gap", [previous["id"]])
    elif not cues and duration_ms > 0:
        add(0, duration_ms, "gap", [])
    return sorted(findings, key=lambda f: (f["start_ms"], f["end_ms"], f["code"]))


def timing_regions(document: dict, duration_ms: int) -> list[dict]:
    cues = sorted(document["segments"], key=lambda c: (c["start_ms"], c["end_ms"], c["id"]))
    by_id = {cue["id"]: cue for cue in cues}
    seeds = []
    for finding in timing_findings(document, duration_ms):
        related = [by_id[cue_id] for cue_id in finding["cue_ids"]]
        seeds.append((min([finding["start_ms"]] + [c["start_ms"] for c in related]),
                      max([finding["end_ms"]] + [c["end_ms"] for c in related]), finding["code"]))
    merged = []
    for start, end, reason in sorted(seeds):
        start, end = max(0, start - CONTEXT_MS), min(duration_ms, end + CONTEXT_MS)
        if merged and start <= merged[-1]["end_ms"]:
            merged[-1]["end_ms"] = max(end, merged[-1]["end_ms"])
            merged[-1]["codes"].add(reason)
        else:
            merged.append({"start_ms": start, "end_ms": end, "codes": {reason}})
    # Include whole cues within each window; boundary-crossing cues are read-only context.
    result = []
    cursor = 0
    for region in merged:
        while cursor < len(cues) and cues[cursor]["start_ms"] < region["start_ms"]:
            cursor += 1
        selected = []
        locked = 0
        while cursor < len(cues) and cues[cursor]["start_ms"] < region["end_ms"]:
            cue = cues[cursor]
            if cue["end_ms"] <= region["end_ms"]:
                if cue.get("locked"):
                    locked += 1
                else:
                    selected.append(cue["id"])
            cursor += 1
        result.append({"id": f"timing-{region['start_ms']}-{region['end_ms']}",
                       "start_ms": region["start_ms"], "end_ms": region["end_ms"],
                       "cue_ids": selected, "locked_count": locked,
                       "reasons": [REASONS[code] for code in sorted(region["codes"])]})
    return result


def validate_retiming(before: list[dict], after: list[dict]) -> None:
    changed = False
    for original, updated in zip(before, after, strict=True):
        if original.get("locked"):
            raise ValueError("Không được đặt lại timing cue đã khóa.")
        for field in ("text", "source_text", "secondary_text", "source_language", "content_source"):
            if original.get(field) != updated.get(field):
                raise ValueError("Đặt lại timing phải giữ nguyên lời dịch, lời gốc và nguồn của từng cue.")
        changed |= (original["start_ms"], original["end_ms"]) != (updated["start_ms"], updated["end_ms"])
    if not changed:
        raise ValueError("Đề xuất không thay đổi timing; trả danh sách rỗng khi không có bằng chứng cần sửa.")
