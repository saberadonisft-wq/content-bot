"""Measure serial FFmpeg/cache cost on existing WAV copies, without ASR/TTS or project edits."""
import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.voiceover import processed_audio
from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.audio_cache import AudioCache
from app.services.voiceover.store import write_json


def peak_python_rss():
    if sys.platform == 'win32':
        import ctypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong),
                *[(name, ctypes.c_size_t) for name in ('peak', 'working', 'peak_paged', 'paged',
                    'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.peak
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('samples', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--count', type=int, default=8, choices=range(1, 13))
    args = parser.parse_args()
    paths = sorted(args.samples.glob('*.wav'))[:args.count]
    if len(paths) != args.count:
        raise ValueError('Not enough WAV samples')
    args.output.mkdir(parents=True, exist_ok=False)
    originals = {path: audio_metadata(path) for path in paths}
    ffmpeg_calls = []
    original_ffmpeg = processed_audio.ffmpeg

    def measured_ffmpeg(*params, **kwargs):
        started = time.perf_counter()
        result = original_ffmpeg(*params, **kwargs)
        ffmpeg_calls.append(time.perf_counter() - started)
        return result

    processed_audio.ffmpeg = measured_ffmpeg
    phases = []
    try:
        for name in ('create', 'reuse'):
            started, cpu_started, call_count = time.perf_counter(), time.process_time(), len(ffmpeg_calls)
            rows = []
            for path, meta in originals.items():
                before = time.perf_counter()
                result = processed_audio.prepare_audio(path, checksum=meta['checksum'], trim_start_ms=0,
                    trim_end_ms=0, rate=1.08, cache_dir=args.output / 'audio')
                rows.append({'source': path.name, 'source_duration_ms': meta['duration_ms'], 'output_id': result['id'],
                    'output_duration_ms': result['duration_ms'], 'cache_hit': result['cache_hit'],
                    'wall_seconds': time.perf_counter() - before})
            phases.append({'phase': name, 'wall_seconds': time.perf_counter() - started,
                'python_cpu_seconds': time.process_time() - cpu_started,
                'ffmpeg_calls': len(ffmpeg_calls) - call_count, 'rows': rows})
    finally:
        processed_audio.ffmpeg = original_ffmpeg
    preserved = all(hashlib.sha256(path.read_bytes()).hexdigest() == meta['checksum'] for path, meta in originals.items())
    report = {'scope': 'WAV conversion/cache only; zero trimming, 1.08x; no ASR, TTS or project updates',
        'phases': phases, 'originals_unchanged': preserved, 'python_peak_rss_bytes': peak_python_rss(),
        'ffmpeg_call_median_seconds': statistics.median(ffmpeg_calls),
        'cache': AudioCache(args.output / 'audio').trim(),
        'limitations': 'Single sequential run; CPU/RSS are Python only, excluding FFmpeg; OS cache not controlled; no quality inference'}
    write_json(args.output / 'measurement.json', report)
    assert preserved and phases[0]['ffmpeg_calls'] == len(paths) and phases[1]['ffmpeg_calls'] == 0
    assert all(row['cache_hit'] for row in phases[1]['rows'])
    print(json.dumps({**report, 'phases': [{k: v for k, v in phase.items() if k != 'rows'} for phase in phases]}, indent=2))


if __name__ == '__main__':
    main()
