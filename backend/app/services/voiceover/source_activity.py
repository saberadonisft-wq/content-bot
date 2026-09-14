"""Bounded source-window VAD observations; no transcript or automatic gap filling."""
from __future__ import annotations

import threading
from pathlib import Path

from ..gemini_media import VAD_SHA256, SileroStream
from ..speech_evidence import audio_identity
from ..subtitle_alignment import AudioWindow, _extract_pcm_window
from .store import digest, read_json, write_json

VERSION = 'source-window-vad-v1'


def observe_source_activity(video: Path, media: dict, windows: list[tuple[int, int]], *,
                            cache_dir: Path, cancel: threading.Event | None = None,
                            max_audio_ms: int = 180_000) -> dict:
    """One CPU model reused across <=30s windows, with an explicit total budget."""
    import numpy as np

    if not media.get('has_audio'):
        return {'windows': [], 'skipped': [], 'reason': 'no_audio', 'speech_verified': False}
    if not audio_identity(media):
        raise ValueError('Thiếu định danh audio nguồn.')
    if len(windows) > 500 or not 0 < max_audio_ms <= 600_000:
        raise ValueError('Vượt ngân sách phân tích audio nguồn.')
    cancel = cancel or threading.Event()
    stat = video.stat()
    identity = {'audio_identity': audio_identity(media), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'algorithm': VERSION, 'vad_sha256': VAD_SHA256}
    total = 0
    model = None
    results, skipped = [], []
    for start, end in sorted(set(windows)):
        if cancel.is_set():
            raise InterruptedError('Đã hủy kiểm tra audio nguồn')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= media['duration_ms']:
            raise ValueError('Cửa sổ lời nói nằm ngoài video.')
        if end - start > 30_000 or total + end - start > max_audio_ms:
            skipped.append({'start_ms': start, 'end_ms': end, 'reason': 'audio_budget'})
            continue
        total += end - start
        key = digest({**identity, 'start_ms': start, 'end_ms': end})
        path = cache_dir / f'{key}.json'
        try:
            cached = read_json(path)
            if (cached['identity'] == identity and cached['start_ms'] == start and cached['end_ms'] == end
                    and isinstance(cached['speech'], list) and all(
                        len(pair) == 2 and all(type(v) is int for v in pair) and start <= pair[0] < pair[1] <= end
                        for pair in cached['speech'])):
                results.append({**cached, 'cache_hit': True})
                continue
        except (OSError, ValueError, TypeError, KeyError):
            pass
        pcm = _extract_pcm_window(video, AudioWindow(start, end, ()), cancel_event=cancel, timeout_seconds=30)
        frames = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        expected = round((end - start) * 16)
        if abs(len(frames) - expected) > 512:
            skipped.append({'start_ms': start, 'end_ms': end, 'reason': 'incomplete_audio'})
            continue
        if model is None:
            model = SileroStream()
        model.reset()
        intervals, active, silence = [], None, None
        for offset in range(0, len(frames), 16_000):
            if cancel.is_set():
                raise InterruptedError('Đã hủy kiểm tra audio nguồn')
            chunk = frames[offset:offset + 16_000]
            for timestamp, probability in model.feed(chunk, final=offset + len(chunk) >= len(frames)):
                if probability >= 0.5:
                    silence = None
                    if active is None:
                        active = max(0, timestamp - 32)
                elif active is not None and probability < 0.35:
                    if silence is None:
                        silence = timestamp
                    if timestamp - silence >= 96:
                        upper = min(end, start + silence + 32)
                        if start + active < upper:
                            intervals.append([start + active, upper])
                        active = silence = None
        if active is not None and start + active < end:
            intervals.append([start + active, end])
        if (video.stat().st_size, video.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError('Video đã thay đổi trong lúc kiểm tra.')
        result = {'identity': identity, 'start_ms': start, 'end_ms': end, 'speech': intervals,
                  'grid_ms': 32, 'speech_verified': False, 'automatic_borrow_allowed': False,
                  'cache_hit': False}
        write_json(path, result)
        results.append(result)
    return {'windows': results, 'skipped': skipped, 'analyzed_audio_ms': total,
            'speech_verified': False, 'algorithm': VERSION}


def classify_gap(start_ms: int, end_ms: int, observations: dict) -> str:
    """A cue gap is unknown until a full observation covers it; VAD is still a candidate."""
    if end_ms <= start_ms:
        raise ValueError('Khoảng trống phải có thời lượng dương.')
    for window in observations['windows']:
        if window['start_ms'] <= start_ms and window['end_ms'] >= end_ms:
            if any(a < end_ms and b > start_ms for a, b in window['speech']):
                return 'speech_in_gap_candidate'
            return 'quiet_gap_candidate'
    return 'unobserved_gap'
