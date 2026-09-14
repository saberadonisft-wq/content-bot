"""Render and recheck one bounded sync candidate without changing the project."""
from __future__ import annotations

import math
from contextlib import ExitStack
from pathlib import Path

from .dubbed_speech import inspect_dubbed_speech
from .processed_audio import prepare_audio
from .store import normalized_text
from .timing import file_interval


def neighbour_bounds(voice, clip, source_start: int, video_duration: int):
    """Use actual playback positions, including equal cue starts and negative offsets."""
    intervals = [file_interval(other) for other in voice.clips if other.id != clip.id]
    previous = max((end for start, end in intervals if start < source_start), default=0)
    following = min((start for start, _ in intervals if start >= source_start), default=video_duration)
    return previous, following


def prepare_sync_candidate(voice, clip, proposal: dict, *, source: Path, checksum: str,
                           video_duration_ms: int, cache_dir: Path, model_dir: Path,
                           whisper_model: str = 'small', cancel=None) -> dict:
    if proposal['state'] != 'candidate' or not proposal.get('dubbed_speech_verified'):
        raise ValueError('Phương án chưa đủ bằng chứng để xử lý audio.')
    if clip.sync.timing_locked or clip.sync.timing_origin != 'automatic':
        raise ValueError('Mốc giọng đã khóa; không tạo phương án tự động.')
    with ExitStack() as leases:
        prepared = prepare_audio(source, checksum=checksum, trim_start_ms=proposal['trim_start_ms'],
            trim_end_ms=proposal['trim_end_ms'], rate=proposal['suggested_rate'],
            cache_dir=cache_dir / 'audio', cancel=cancel, lease_stack=leases)
        output = cache_dir / 'audio' / prepared['filename']
        inspected = inspect_dubbed_speech(output, normalized_text(clip.spoken_text, voice.pronunciation), checksum=prepared['checksum'],
            cache_dir=cache_dir / 'evidence', model_dir=model_dir, whisper_model=whisper_model, cancel=cancel)
    result = {'audio': prepared, 'inspection': inspected, 'issues': list(inspected['issues']),
              'state': 'needs_review', 'playback_rate': 1, 'applied': False}
    if not inspected.get('speech_verified'):
        return result
    speech = inspected['speech_evidence']
    if not 0 <= speech['start_ms'] < speech['end_ms'] <= prepared['duration_ms']:
        result['issues'].append('processed_speech_bounds_invalid')
        return result
    # Re-anchor using measured output speech, not duration/rate arithmetic.
    start = math.ceil(proposal['source_start_ms'] - speech['start_ms'])
    end = start + prepared['duration_ms']
    speech_start, speech_end = start + speech['start_ms'], start + speech['end_ms']
    previous, following = neighbour_bounds(voice, clip, proposal['source_start_ms'], video_duration_ms)
    if start < 0 or end > video_duration_ms:
        result['issues'].append('outside_video')
    if start < previous or end > following:
        result['issues'].append('overlap')
    if speech_end > proposal['allowed_end_ms'] + 1:
        result['issues'].append('processed_speech_ends_late')
    result.update(offset_ms=start - clip.start_ms, file_start_ms=start, file_end_ms=end,
                  speech_start_ms=speech_start, speech_end_ms=speech_end,
                  output_speech_verified=True)
    result['state'] = 'ready_for_review' if not result['issues'] else 'needs_review'
    return result
