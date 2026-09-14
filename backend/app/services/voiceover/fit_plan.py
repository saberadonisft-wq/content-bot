"""Plan trim and tempo against source speech, preserving every neighbouring onset."""
from __future__ import annotations

import math

MAX_RATE = 1.15
MAX_BORROW_MS = 250


def plan_source_fit(clip, source_start: int, source_end: int, *, video_duration_ms: int,
                    previous_file_end_ms: float = 0, next_file_start_ms: float | None = None,
                    audio: dict | None = None, verified_tail_ms: int = 0) -> dict:
    """Server-only proposal; callers must bind source/audio evidence before applying.

    Speech boundaries belong to the original WAV; offsets returned here are absolute
    relative to the subtitle start, never increments on the previous offset.
    """
    issues = []
    duration = float(clip.duration_ms)
    trim_start = trim_end = speech_head = 0.0
    speech_tail = duration
    verified = bool(audio and audio.get('speech_verified') and audio.get('speech_evidence'))
    if verified:
        evidence = audio['speech_evidence']
        speech_head, speech_tail = evidence['start_ms'], evidence['end_ms']
        if audio.get('automatic_trim_allowed'):
            trim_start, trim_end = audio['trim_start_ms'], audio['trim_end_ms']
    numbers = [duration, source_start, source_end, video_duration_ms, previous_file_end_ms,
               trim_start, trim_end, speech_head, speech_tail, verified_tail_ms]
    if next_file_start_ms is not None:
        numbers.append(next_file_start_ms)
    if any(not math.isfinite(value) for value in numbers):
        raise ValueError('Mốc căn giọng phải là số hữu hạn.')
    if (not 0 <= source_start < source_end <= video_duration_ms
            or not 0 <= trim_start <= speech_head < speech_tail <= duration - trim_end
            or trim_end < 0):
        raise ValueError('Biên nói hoặc phần đệm không hợp lệ.')
    if clip.sync.timing_locked or clip.sync.timing_origin != 'automatic':
        issues.append('timing_locked')
    if clip.rate > MAX_RATE:
        issues.append('existing_fast_rate_needs_review')
    if not clip.asset_id or clip.status == 'stale':
        issues.append('audio_not_ready')
    if not verified:
        issues.append('dubbed_transcript_unverified')
    file_limit = min(video_duration_ms, next_file_start_ms if next_file_start_ms is not None else video_duration_ms)
    speech_limit = min(file_limit, source_end + min(MAX_BORROW_MS, max(0, verified_tail_ms)))
    head = speech_head - trim_start
    remaining = duration - trim_start - trim_end
    spoken_duration = speech_tail - speech_head
    budgets = [(spoken_duration, speech_limit - source_start),
               (remaining - head, file_limit - source_start)]
    if head:
        budgets.append((head, source_start - max(0, previous_file_end_ms)))
    elif source_start < previous_file_end_ms:
        issues.append('overlap')
    rates = [1.0]
    for length, budget in budgets:
        if budget <= 0:
            issues.append('source_overlap')
        else:
            rates.append(length / budget)
    needed = max(rates)
    if needed > MAX_RATE:
        issues.append('duration_not_feasible')
    rate = min(needed, MAX_RATE)
    # Integer placement differs by <1 ms from the analytical solution.
    file_start = math.ceil(source_start - head / rate)
    file_end = file_start + remaining / rate
    actual_speech_start = file_start + head / rate
    actual_speech_end = file_start + (speech_tail - trim_start) / rate
    if file_start < 0 or file_end > video_duration_ms + 1:
        issues.append('outside_video')
    if file_start < previous_file_end_ms or file_end > file_limit + 1:
        issues.append('overlap')
    return {'state': 'blocked' if issues else 'candidate', 'blocked_reasons': list(dict.fromkeys(issues)),
            'source_start_ms': source_start, 'source_end_ms': source_end,
            'allowed_start_ms': max(0, previous_file_end_ms), 'allowed_end_ms': speech_limit,
            'suggested_offset_ms': file_start - clip.start_ms, 'suggested_rate': rate, 'required_rate': needed,
            'trim_start_ms': trim_start, 'trim_end_ms': trim_end,
            'predicted_file_start_ms': file_start, 'predicted_file_end_ms': file_end,
            'predicted_speech_start_ms': actual_speech_start, 'predicted_speech_end_ms': actual_speech_end,
            'borrowed_ms': max(0, actual_speech_end - source_end),
            'dubbed_speech_verified': verified, 'output_measured': False}
