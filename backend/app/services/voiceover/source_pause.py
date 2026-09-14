"""Permit a small tail only when source context, VAD and quiet PCM agree."""
from __future__ import annotations

import math
import tempfile
import wave
from pathlib import Path

from ..subtitle_alignment import AudioWindow, _extract_pcm_window
from .quiet_analysis import analyze_quiet_edges
from .source_activity import observe_source_activity


def verified_tail(video, media, source_end, following_start, context, *, cache_dir, cancel=None):
    decision = context['decision']
    if (not decision['source_is_clear'] or decision['confidence'] < .9
            or not decision['tail_pause_has_no_semantic_function']
            or not decision['no_scene_or_speaker_change_at_tail']):
        return {'allowed_ms': 0, 'reason': 'context_pause_unverified'}
    guard = 80
    amount = min(250, math.floor(following_start - source_end - guard), media['duration_ms'] - source_end - guard)
    if amount <= 0:
        return {'allowed_ms': 0, 'reason': 'no_guarded_pause'}
    end = source_end + amount + guard
    observations = observe_source_activity(video, media, [(source_end, end)], cache_dir=cache_dir / 'activity',
                                           cancel=cancel, max_audio_ms=1000)
    windows = observations['windows']
    if not windows or any(window['speech'] for window in windows):
        return {'allowed_ms': 0, 'reason': 'source_pause_has_speech'}
    pcm = _extract_pcm_window(video, AudioWindow(source_end, end, ()), cancel_event=cancel, timeout_seconds=10)
    if len(pcm) != (end - source_end) * 32:
        return {'allowed_ms': 0, 'reason': 'source_pause_audio_incomplete'}
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pause-', dir=cache_dir) as temporary:
        path = Path(temporary) / 'pause.wav'
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(pcm)
        quiet = analyze_quiet_edges(path, cancel=cancel)
    if quiet['has_detectable_signal']:
        return {'allowed_ms': 0, 'reason': 'source_pause_not_quiet'}
    return {'allowed_ms': amount, 'guard_ms': guard, 'quiet': quiet,
            'activity': observations, 'context': context, 'reason': 'source_pause_verified'}
