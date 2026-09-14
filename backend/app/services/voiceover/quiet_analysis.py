"""Cheap edge-quiet measurements. Results are candidates, never permission to cut speech."""
from __future__ import annotations

import hashlib
import math
import threading
import wave
from pathlib import Path

from .store import read_json, write_json

VERSION = 'edge-rms-10ms-v1'
THRESHOLDS_DB = (-50, -40)


def analyze_quiet_edges(path: Path, *, expected_checksum: str | None = None,
                        cache_dir: Path | None = None, cancel: threading.Event | None = None) -> dict:
    def check():
        if cancel and cancel.is_set():
            raise InterruptedError('Đã hủy đo audio')

    check()
    initial = path.stat()
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(1024 * 1024):
            check()
            checksum.update(data)
    key = checksum.hexdigest()
    if expected_checksum and key != expected_checksum:
        raise ValueError('Audio đã thay đổi; không dùng mốc đo cũ.')
    cache_path = cache_dir / f'{key}-{VERSION}.json' if cache_dir else None
    if cache_path and cache_path.is_file():
        try:
            cached = read_json(cache_path)
            if cached['checksum'] == key and cached['algorithm'] == VERSION:
                return {**cached, 'cache_hit': True}
        except (OSError, ValueError, TypeError, KeyError):
            pass
    # numpy is already a backend dependency; no ONNX/Torch/model loading here.
    import numpy as np

    observed = {level: {'first': None, 'last': None} for level in THRESHOLDS_DB}
    peak_first = peak_last = None
    frames_read = 0
    with wave.open(str(path), 'rb') as audio:
        rate, channels, width, frames = audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()
        if channels != 1 or width != 2 or rate not in {16000, 22050, 24000, 44100, 48000} or not 0 < frames <= rate * 600:
            raise ValueError('Đo audio yêu cầu PCM16 mono, tối đa 10 phút mỗi đoạn.')
        # At 22050 Hz the frame grid is 220 samples, not an exact 10 ms claim.
        step = max(1, round(rate / 100))
        while raw := audio.readframes(step):
            check()
            values = np.frombuffer(raw, dtype='<i2').astype(np.float64) / 32768
            first, last = frames_read, frames_read + len(values)
            rms = math.sqrt(float(np.mean(values * values)))
            for level, interval in observed.items():
                if rms >= 10 ** (level / 20):
                    if interval['first'] is None:
                        interval['first'] = first
                    interval['last'] = last
            if float(np.max(np.abs(values))) >= 10 ** (-60 / 20):
                if peak_first is None:
                    peak_first = first
                peak_last = last
            frames_read = last
    check()
    final = path.stat()
    if frames_read != frames or (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise ValueError('Audio bị cắt hoặc thay đổi trong lúc đo.')
    thresholds = []
    for level, interval in observed.items():
        head = interval['first'] * 1000 / rate if interval['first'] is not None else None
        tail = (frames - interval['last']) * 1000 / rate if interval['last'] is not None else None
        thresholds.append({'threshold_dbfs': level, 'head_quiet_ms': head, 'tail_quiet_ms': tail})
    # Low peak guard retains even weak consonants; still not linguistic validation.
    duration = frames * 1000 / rate
    head = peak_first * 1000 / rate if peak_first is not None else None
    tail = (frames - peak_last) * 1000 / rate if peak_last is not None else None
    result = {'algorithm': VERSION, 'checksum': key, 'duration_ms': duration,
              'grid_ms': step * 1000 / rate, 'thresholds': thresholds,
              'protected_head_candidate_ms': max(0, head - 30) if head is not None else 0,
              'protected_tail_candidate_ms': max(0, tail - 30) if tail is not None else 0,
              'has_detectable_signal': peak_first is not None,
              'speech_verified': False, 'automatic_trim_allowed': False, 'cache_hit': False}
    if cache_path:
        write_json(cache_path, result)
    return result
