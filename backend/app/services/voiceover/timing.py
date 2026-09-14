"""Deterministic timeline checks; being inside a cue is not source alignment."""
from __future__ import annotations

import json

from .models import VoiceDocument

TOLERANCE_MS = 2
AUTO_RATE_LIMIT = 1.15


def source_signature(cue: dict) -> str:
    return json.dumps([cue.get(key) for key in ('id', 'start_ms', 'end_ms', 'text', 'source_text',
        'secondary_text', 'source_language', 'revision')], ensure_ascii=False, separators=(',', ':'))


def clip_signature(clip) -> str:
    return json.dumps([clip.id, clip.source_cue_ids, clip.start_ms, clip.end_ms, clip.offset_ms,
        clip.spoken_text, clip.source_text, clip.asset_id, clip.duration_ms], ensure_ascii=False, separators=(',', ':'))


def current_alignment(clip):
    alignment = clip.sync.alignment
    return alignment if alignment and clip.rate == 1 and alignment.clip_signature == clip_signature(clip) else None


def verify_source_bindings(doc: VoiceDocument, cues: list[dict]) -> None:
    index = {cue['id']: cue for cue in cues}
    for clip in doc.clips:
        alignment = current_alignment(clip)
        if alignment:
            cue = index.get(clip.source_cue_ids[0]) if len(clip.source_cue_ids) == 1 else None
            if not cue or source_signature(cue) != alignment.source_signature:
                raise ValueError('Phụ đề nguồn đã đổi sau khi căn giọng. Kiểm tra lại trước khi xuất.')


def file_interval(clip) -> tuple[float, float]:
    start = clip.start_ms + clip.offset_ms
    alignment = current_alignment(clip)
    duration = alignment.output_duration_ms if alignment else (
        clip.duration_ms / clip.rate if clip.duration_ms else clip.end_ms - clip.start_ms)
    return start, start + duration


def window_issues(clip) -> list[str]:
    if not clip.asset_id or not clip.duration_ms:
        return []
    start, end = file_interval(clip)
    issues = []
    alignment = current_alignment(clip)
    lower, upper = clip.start_ms, clip.end_ms
    if alignment:
        start += alignment.speech_head_ms
        end = file_interval(clip)[0] + alignment.speech_tail_ms
        lower, upper = alignment.source_start_ms, alignment.allowed_end_ms
    if start < lower - TOLERANCE_MS:
        issues.append("starts_early")
    if end > upper + TOLERANCE_MS:
        issues.append("ends_late")
    return issues


def refresh_timing(doc: VoiceDocument) -> VoiceDocument:
    for clip in doc.clips:
        if not current_alignment(clip):
            clip.sync.alignment = None
        issues = window_issues(clip)
        if not clip.asset_id or not clip.duration_ms:
            issues.append("missing_audio")
        verified = current_alignment(clip) is not None and clip.status != 'stale'
        clip.sync.issues = issues if verified else [*issues, "source_unverified"]
        clip.sync.state = "needs_review" if issues else ('aligned' if verified else "unverified")
        if clip.status in {"ready", "overflow"} and clip.asset_id:
            clip.status = "overflow" if window_issues(clip) else "ready"
    maximum_end = float("-inf")
    maximum_clip = None
    for clip in sorted(doc.clips, key=lambda c: file_interval(c)[0]):
        if not clip.asset_id or not clip.duration_ms or clip.status == "stale":
            continue
        start, end = file_interval(clip)
        if start < maximum_end - TOLERANCE_MS:
            for overlapping in (clip, maximum_clip):
                if overlapping and "overlap" not in overlapping.sync.issues:
                    overlapping.sync.issues.append("overlap")
                    overlapping.sync.state = "needs_review"
        if end > maximum_end:
            maximum_end, maximum_clip = end, clip
    return doc
