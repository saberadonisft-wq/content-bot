"""Cross-check TTS edges against its actual transcript before proposing silence removal."""
from __future__ import annotations

import math
import threading
from pathlib import Path

from ..speech_evidence import valid_speech_evidence
from ..subtitle_alignment import (
    AlignmentSettings,
    _normalized_word,
    align_subtitle_document,
)
from .quiet_analysis import analyze_quiet_edges
from .source_activity import observe_source_activity

VERSION = 'dubbed-speech-edges-v1'
GUARD_MS = 80


def inspect_dubbed_speech(path: Path, spoken_text: str, *, checksum: str, model_dir: Path,
                          cache_dir: Path, whisper_model: str = 'small',
                          cancel: threading.Event | None = None) -> dict:
    """Called only inside the supervised sync worker, on bounded problem clips.

    A passing result is automatic evidence, not human-confirmed ground truth.
    The original file stays intact; stage 3 decides whether an edge trim is useful.
    """
    quiet = analyze_quiet_edges(path, expected_checksum=checksum, cache_dir=cache_dir / 'quiet', cancel=cancel)
    result = {'algorithm': VERSION, 'checksum': checksum, 'quiet': quiet, 'issues': [],
              'trim_start_ms': 0, 'trim_end_ms': 0, 'automatic_trim_allowed': False,
              'speech_verified': False, 'guard_ms': GUARD_MS}
    if not spoken_text.strip() or not quiet['has_detectable_signal']:
        result['issues'].append('dubbed_speech_missing')
        return result
    if quiet['duration_ms'] > 30_000:
        result['issues'].append('dubbed_audio_budget')
        return result
    duration = math.ceil(quiet['duration_ms'])
    media = {'has_audio': True, 'duration_ms': duration, 'audio_hash': checksum, 'fingerprint': checksum}
    cue = {'id': 'dubbed', 'start_ms': 0, 'end_ms': duration, 'text': spoken_text,
           'source_text': spoken_text, 'source_language': 'vi', 'content_source': 'audio',
           'timing_source': 'gemini_estimate', 'timing_precision_ms': 1, 'revision': 0}
    aligned = align_subtitle_document(path, {'language': 'vi', 'segments': [cue]}, media,
        settings=AlignmentSettings(engine='faster_whisper', preserve_display=True, window_padding_ms=0,
            max_shift_ms=duration, whisper_model=whisper_model, whisper_model_dir=model_dir,
            whisper_allow_download=False, whisper_device='cpu', whisper_compute_type='int8', cpu_threads=3),
        cache_dir=cache_dir / 'transcript', cancel_event=cancel)
    output = aligned['document']['segments'][0]
    words = [word for window in aligned.get('source_observations', []) for word in window['words']]
    evidence = valid_speech_evidence(output, media)
    # Independent ASR must hear the complete utterance, including negation and repeated syllables.
    heard = ' '.join(word['text'] for word in words)
    result['observed_text'] = heard
    result['words'] = words
    result['warnings'] = aligned['warnings']
    result['diagnostics'] = {
        'transcript_matches': bool(words) and _normalized_word(heard) == _normalized_word(spoken_text),
        'minimum_confidence': min((word['confidence'] for word in words), default=0),
        'low_confidence_words': [word['text'] for word in words if word['confidence'] < .65][:10],
        'alignment_valid': bool(evidence and evidence.method == 'asr_observed'
            and evidence.transcript_complete and not output.get('needs_review')),
    }
    if (not evidence or evidence.method != 'asr_observed' or not evidence.transcript_complete
            or output.get('needs_review') or not words
            or _normalized_word(heard) != _normalized_word(spoken_text)
            or any(word['confidence'] < .65 for word in words)):
        result['issues'].append('dubbed_transcript_unverified')
        return result
    activity = observe_source_activity(path, media, [(0, duration)],
        cache_dir=cache_dir / 'activity', cancel=cancel, max_audio_ms=30_000)
    result['activity'] = activity
    windows = [window for window in activity['windows'] if window['start_ms'] == 0 and window['end_ms'] == duration]
    if not windows or not windows[0]['speech']:
        result['issues'].append('dubbed_activity_unverified')
        return result
    vad_start = min(pair[0] for pair in windows[0]['speech'])
    vad_end = max(pair[1] for pair in windows[0]['speech'])
    trim_start = math.floor(max(0, min(quiet['protected_head_candidate_ms'],
                                     evidence.start_ms - GUARD_MS, vad_start - GUARD_MS)))
    trim_end = math.floor(max(0, min(quiet['protected_tail_candidate_ms'],
                                   quiet['duration_ms'] - evidence.end_ms - GUARD_MS,
                                   quiet['duration_ms'] - vad_end - GUARD_MS)))
    # Revalidate bytes after inference; never bind the result to a file replaced mid-job.
    analyze_quiet_edges(path, expected_checksum=checksum, cache_dir=cache_dir / 'quiet', cancel=cancel)
    result.update(speech_evidence=evidence.model_dump(), trim_start_ms=trim_start, trim_end_ms=trim_end,
                  automatic_trim_allowed=bool(trim_start or trim_end), speech_verified=True,
                  human_verified=False)
    return result
