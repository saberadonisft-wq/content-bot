"""Immutable, lazy trim/tempo WAVs. Original audio and clip timing stay untouched."""
from __future__ import annotations

import math
import threading
import uuid
import wave
from contextlib import ExitStack
from pathlib import Path

from .audio import audio_metadata
from .audio_cache import MAX_BYTES, MAX_ENTRIES, AudioCache
from .mix import ffmpeg, tempo_filter
from .store import digest, read_json, write_json

VERSION = 'trim-tempo-pcm16-v1'
_processing = threading.Lock()


def prepare_audio(source: Path, *, checksum: str, trim_start_ms: float, trim_end_ms: float,
                  rate: float, cache_dir: Path, cancel: threading.Event | None = None,
                  lease_stack: ExitStack | None = None, max_cache_bytes: int = MAX_BYTES,
                  max_cache_entries: int = MAX_ENTRIES) -> dict:
    """Caller supplies server-verified trim bounds, never arbitrary client proposals.

    Gain is not baked. Placement is deliberately absent from the key so moving a
    clip doesn't trigger another waveform conversion. Preview must play this at 1x.
    """
    if any(not math.isfinite(value) for value in (trim_start_ms, trim_end_ms, rate)):
        raise ValueError('Thông số xử lý audio không hợp lệ.')
    if not .95 <= rate <= 1.15 or min(trim_start_ms, trim_end_ms) < 0:
        raise ValueError('Thông số xử lý vượt giới hạn tự động.')
    while not _processing.acquire(timeout=.1):
        if cancel and cancel.is_set():
            raise InterruptedError('Đã hủy chuẩn bị audio')
    temporary = None
    resources = ExitStack()
    try:
        cache = AudioCache(cache_dir, max_bytes=max_cache_bytes, max_entries=max_cache_entries)
        resources.enter_context(cache.encoder(cancel))
        cache.clean_abandoned_parts()
        if cancel and cancel.is_set():
            raise InterruptedError('Đã hủy chuẩn bị audio')
        meta = audio_metadata(source)
        if meta['checksum'] != checksum:
            raise ValueError('Audio gốc đã thay đổi; cần kiểm tra lại trước khi xử lý.')
        with wave.open(str(source), 'rb') as wav:
            sample_rate, frame_count = wav.getframerate(), wav.getnframes()
        # Cut only whole samples wholly within the permitted trim interval.
        first = math.floor(trim_start_ms * sample_rate / 1000)
        last = frame_count - math.floor(trim_end_ms * sample_rate / 1000)
        if not 0 <= first < last <= frame_count:
            raise ValueError('Phần đệm cắt bỏ vượt thời lượng audio.')
        specification = {'algorithm': VERSION, 'source_checksum': checksum, 'first_sample': first,
                         'last_sample': last, 'sample_rate': sample_rate, 'rate': rate}
        key = digest(specification)
        (lease_stack or resources).enter_context(cache.pin(key))
        output, manifest = cache_dir / f'{key}.wav', cache_dir / f'{key}.json'
        cached = None
        if output.exists() and manifest.exists():
            try:
                record = read_json(manifest)
                actual = audio_metadata(output)
                with wave.open(str(output), 'rb') as wav:
                    exact_duration = wav.getnframes() * 1000 / wav.getframerate()
                if (record['specification'] == specification and record['checksum'] == actual['checksum']
                        and abs(record['duration_ms'] - exact_duration) < .000001):
                    cached = record
            except (OSError, ValueError, KeyError, EOFError, wave.Error):
                pass
        if cached:
            return {**cached, 'cache_hit': True, 'cache': cache.trim()}
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Reserve PCM plus filter padding and waveform metadata before starting FFmpeg.
        cache.trim(reserve_bytes=math.ceil((last - first) * 2 / rate) + 512 * 1024, new_key=key)
        temporary = cache_dir / f'{key}.{uuid.uuid4().hex}.part.wav'
        filters = f'atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS,' + tempo_filter(rate)
        ffmpeg(['-threads', '1', '-i', str(source), '-vn', '-af', filters.rstrip(','),
                '-filter_threads', '1', '-ac', '1', '-ar', str(sample_rate), '-c:a', 'pcm_s16le',
                str(temporary)], cancel=cancel, timeout=60)
        measured = audio_metadata(temporary)
        with wave.open(str(temporary), 'rb') as wav:
            measured_duration = wav.getnframes() * 1000 / wav.getframerate()
        if audio_metadata(source)['checksum'] != checksum:
            raise ValueError('Audio gốc thay đổi trong lúc xử lý.')
        if cancel and cancel.is_set():
            raise InterruptedError('Đã hủy chuẩn bị audio')
        temporary.replace(output)
        record = {'id': key, 'filename': output.name, 'specification': specification,
                  **measured, 'duration_ms': measured_duration, 'playback_rate': 1,
                  'output_measured': True, 'output_speech_verified': False, 'cache_hit': False}
        write_json(manifest, record)
        return {**record, 'cache': cache.trim()}
    finally:
        try:
            if temporary:
                temporary.unlink(missing_ok=True)
        finally:
            try:
                resources.close()
            finally:
                _processing.release()
