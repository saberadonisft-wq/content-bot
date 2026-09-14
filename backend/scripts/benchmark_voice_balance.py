"""Bounded local TTS comparison. Writes samples separately from project/checkpoint data."""
import argparse
import csv
import json
import os
import runpy
import statistics
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'runtimes' / 'voiceover'


def run(args):
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    # Fixed quantiles, independent of device; bounded representative short/long cues.
    candidates = sorted({row['text'] for row in manifest['clips'] if 12 <= len(row['text']) <= 100}, key=lambda s: (len(s), s))
    texts = [candidates[round(i * (len(candidates) - 1) / (args.count - 1))] for i in range(args.count)]
    os.environ['CONTENT_BOT_VOICE_THREADS'] = str(args.threads)
    os.environ['CONTENT_BOT_VOICE_BATCH_SIZE'] = str(args.batch)
    os.environ['VOICE_WORKER_LOCK'] = str(RUNTIME / '.worker.lock')
    worker = runpy.run_path(str(RUNTIME / 'worker.py'))
    if (manifest['profile'].get('model_id', worker['MODEL']) != worker['MODEL']
            or manifest['profile'].get('model_revision') != worker['REVISION']):
        raise ValueError('This comparison requires the pinned v3 Turbo model.')
    result = {'device': args.device, 'threads': args.threads, 'batch': args.batch,
              'model_revision': worker['REVISION'], 'sdk': worker['SDK'], 'samples': []}
    with worker['worker_slot'](target) as acquired:
        if not acquired:
            return
        started = time.monotonic()
        worker['configure_compute'](args.device)
        engine = worker['load_engine'](args.device)
        voice = worker['voice_arguments'](engine, manifest['profile'], Path(args.manifest).parent)
        result['load_seconds'] = time.monotonic() - started
        import numpy as np
        np.random.seed(42)
        if args.device == 'cuda':
            import torch
            torch.manual_seed(42)
        # Warm up separately; it must not inflate steady-state throughput.
        worker['infer_with_retry'](engine, texts[0], voice, args.device)
        measurements = []
        stopped = threading.Event()

        def monitor():
            while not stopped.is_set():
                try:
                    output = subprocess.check_output([
                        'nvidia-smi', '--query-gpu=power.draw,memory.used,temperature.gpu,utilization.gpu',
                        '--format=csv,noheader,nounits'], text=True, timeout=4,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    row = next(csv.reader(output.splitlines()))
                    measurements.append([float(value.strip()) for value in row])
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                stopped.wait(1)

        observer = threading.Thread(target=monitor, daemon=True)
        observer.start()
        started, cpu_started = time.monotonic(), time.process_time()
        try:
            for offset in range(0, len(texts), args.batch):
                group = [{'id': str(i), 'text': texts[i], 'generation_hash': f'{i:064x}'}
                         for i in range(offset, min(offset + args.batch, len(texts)))]
                before = time.monotonic()
                audios = worker['infer_group'](engine, group, voice, args.device)
                elapsed = time.monotonic() - before
                for item, (audio, error) in zip(group, audios):
                    if error:
                        raise RuntimeError(error)
                    path = target / (item['id'] + '.wav')
                    worker['commit_audio'](audio, engine.sample_rate, path, path.with_suffix('.json'), item, elapsed / len(group))
                    result['samples'].append({'text': item['text'], 'path': str(path),
                                              'audio_seconds': len(audio) / engine.sample_rate})
                print(f'{args.device} threads={args.threads} batch={args.batch}: {len(result["samples"])}/{len(texts)}', flush=True)
            result['seconds'] = time.monotonic() - started
            result['cpu_seconds'] = time.process_time() - cpu_started
        finally:
            stopped.set()
            observer.join(timeout=6)
        result['audio_seconds'] = sum(row['audio_seconds'] for row in result['samples'])
        result['rtf'] = result['seconds'] / result['audio_seconds']
        result['torch_imported'] = 'torch' in __import__('sys').modules
        if measurements:
            result.update(gpu_mean_watts=statistics.mean(row[0] for row in measurements),
                          gpu_peak_memory_mib=max(row[1] for row in measurements),
                          gpu_peak_temperature_c=max(row[2] for row in measurements),
                          gpu_mean_utilization=statistics.mean(row[3] for row in measurements),
                          gpu_telemetry_samples=len(measurements))
        worker['write'](target / 'result.json', result)
        print(json.dumps({k: v for k, v in result.items() if k != 'samples'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--device', choices=['cpu', 'cuda'], required=True)
    parser.add_argument('--threads', type=int, choices=range(1, 9), required=True)
    parser.add_argument('--batch', type=int, choices=range(1, 5), default=1)
    parser.add_argument('--count', type=int, choices=range(2, 33), default=6)
    run(parser.parse_args())
