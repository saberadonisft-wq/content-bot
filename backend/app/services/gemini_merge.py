"""Strict generation payloads and conservative reconciliation in source time."""
from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from itertools import pairwise
from typing import Any

from ..schemas import SubtitleCueV2

CONTENT_SCHEMA_VERSION = 1
MAX_GENERATED_CUES = 20_000
GENERATION_RESPONSE_SCHEMA = {
    "type": "object", "required": ["segments"],
    "properties": {"segments": {"type": "array", "maxItems": 2000, "items": {
        "type": "object", "required": ["start_ms", "end_ms", "text", "source_text", "source_language", "content_source", "needs_review"],
        "properties": {
            "start_ms": {"type": "integer", "minimum": 0}, "end_ms": {"type": "integer", "minimum": 1},
            "text": {"type": "string"}, "source_text": {"type": ["string", "null"]},
            "source_language": {"type": ["string", "null"]},
            "content_source": {"type": "string", "enum": ["audio", "screen", "mixed", "unknown"]},
            "needs_review": {"type": "boolean"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }}},
}


def decode_chunk(raw: str, chunk: dict[str, Any], *, model: str, bilingual: bool) -> list[dict[str, Any]]:
    if len(raw.encode("utf-8")) > 8_000_000:
        raise ValueError("Kết quả đoạn vượt giới hạn 8 MB.")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise TypeError("Gemini không trả danh sách segments hợp lệ.")
    if len(payload["segments"]) > 2000:
        raise ValueError("Kết quả một đoạn vượt giới hạn 2000 cue.")
    duration = chunk["media_end_ms"] - chunk["media_start_ms"]
    cues = []
    for index, item in enumerate(payload["segments"]):
        if not isinstance(item, dict):
            raise TypeError("Cue Gemini không hợp lệ.")
        source_text = item.get("source_text") or item.get("secondary_text")
        cue = SubtitleCueV2.model_validate({**item, "id": f"{chunk['chunk_id']}-{index:04d}",
            "source_text": source_text, "source_language": item.get("source_language") or payload.get("source_language"),
            "secondary_text": source_text if bilingual else None,
            "origin_model": model, "origin_chunk_id": chunk["chunk_id"],
            "timing_source": "gemini_estimate", "locked": False, "revision": 0})
        if cue.end_ms > duration:
            raise ValueError("Timestamp Gemini nằm ngoài media đã gửi.")
        value = cue.model_dump(mode="json")
        if not cue.source_text or not cue.source_language or cue.content_source == "unknown":
            value["needs_review"] = True
        offset = chunk["media_start_ms"] + chunk.get("actual_offset_ms", 0)
        for field in ("start_ms", "end_ms", "speech_start_ms", "speech_end_ms"):
            if value[field] is not None:
                value[field] += offset
        for word in value.get("words") or []:
            word["start_ms"] += offset
            word["end_ms"] += offset
        cues.append(value)
    return cues


def _source(cue):
    # NFKC only: no claim of simplified/traditional or phonetic equivalence.
    return "".join(char for char in unicodedata.normalize("NFKC", cue.get("source_text") or "")
                   if not char.isspace() and not unicodedata.category(char).startswith("P"))


def merge_chunks(results: list[dict[str, Any]], manifest: dict[str, Any]) -> tuple[list[dict], list[dict], list[dict]]:
    cues = [deepcopy(cue) for result in results for cue in result["cues"]]
    by_chunk = {}
    for cue in cues:
        by_chunk.setdefault(cue["origin_chunk_id"], []).append(cue)
    removed = set()
    audit = []
    warnings = []
    chunks = manifest["chunks"]
    for left, right in pairwise(chunks):
        boundary = left["core_end_ms"]
        region_start, region_end = right["media_start_ms"], left["media_end_ms"]
        a = sorted([c for c in by_chunk.get(left["chunk_id"], []) if c["end_ms"] > region_start and c["start_ms"] < region_end], key=lambda c: c["start_ms"])
        b = sorted([c for c in by_chunk.get(right["chunk_id"], []) if c["end_ms"] > region_start and c["start_ms"] < region_end], key=lambda c: c["start_ms"])
        matches = []
        # Exact source sequence matching accommodates 1:many splits. Restrict to a
        # known consistent content channel: OCR over dialogue is not safe to dedupe.
        for i in range(len(a)):
            for j in range(len(b)):
                for na, nb in ((1, 1), (1, 2), (2, 1), (1, 3), (3, 1)):
                    aa, bb = a[i:i + na], b[j:j + nb]
                    if len(aa) != na or len(bb) != nb or any(not _source(c) for c in aa + bb):
                        continue
                    channels = {c.get("content_source", "unknown") for c in aa + bb}
                    languages = {c.get("source_language") for c in aa + bb}
                    if len(channels) != 1 or "unknown" in channels or "mixed" in channels or len(languages) != 1:
                        continue
                    if "".join(map(_source, aa)) != "".join(map(_source, bb)):
                        continue
                    a_start, a_end = aa[0]["start_ms"], max(c["end_ms"] for c in aa)
                    b_start, b_end = bb[0]["start_ms"], max(c["end_ms"] for c in bb)
                    overlap = min(a_end, b_end) - max(a_start, b_start)
                    if overlap <= 0 or overlap * 5 < max(a_end - a_start, b_end - b_start) * 4:
                        continue
                    if max(abs(a_start - b_start), abs(a_end - b_end)) > 300:
                        continue
                    matches.append((aa, bb))
        # Ambiguous matches stay visible. Never choose one of several repeated utterances.
        counts = {}
        for aa, bb in matches:
            for cue in aa + bb:
                counts[cue["id"]] = counts.get(cue["id"], 0) + 1
        for aa, bb in matches:
            if any(counts[c["id"]] != 1 for c in aa + bb):
                continue
            center = (aa[0]["start_ms"] + aa[-1]["end_ms"] + bb[0]["start_ms"] + bb[-1]["end_ms"]) / 4
            kept, dropped = (aa, bb) if center < boundary else (bb, aa)
            removed.update(c["id"] for c in dropped)
            audit.append({"boundary_ms": boundary, "kept": [c["id"] for c in kept],
                          "removed": [c["id"] for c in dropped], "evidence": "exact_source_sequence_same_channel_overlapping_time",
                          "before": dropped})
        unresolved = [c for c in a + b if c["id"] not in removed and not any(c["id"] in entry["kept"] for entry in audit)]
        if unresolved:
            for cue in unresolved:
                cue["needs_review"] = True
            warnings.append({"code": "gemini_boundary_review", "message": "Vùng nối chưa đủ bằng chứng loại trùng; giữ cue để đối chiếu media.",
                             "start_ms": region_start, "end_ms": region_end, "cue_ids": [c["id"] for c in unresolved]})
    combined = sorted((cue for cue in cues if cue["id"] not in removed), key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]))
    if len(combined) > MAX_GENERATED_CUES:
        raise ValueError("Phụ đề vượt giới hạn tài nguyên 20000 cue; hãy xử lý theo phần.")
    return combined, warnings, audit
