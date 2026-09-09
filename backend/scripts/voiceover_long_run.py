"""Real speech endurance benchmark on an isolated three-hour synthetic timeline."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'runtimes/voiceover'))
from benchmark import TEXT
from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.models import VoiceClip, VoiceDocument, VoiceProfile
from app.services.voiceover.store import VoiceStore, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    args = parser.parse_args()
    root = ROOT / 'artifacts/voiceover/endurance' / args.device
    store = VoiceStore(root)
    manager = VoiceManager(store)
    project = 'e' * 20
    started = time.monotonic()
    previous_report = root / 'report.json'
    if previous_report.exists():
        prior = json.loads(previous_report.read_text(encoding='utf-8'))
        previous_job = prior.get('job', {}).get('id')
        if previous_job:
            write_json(root / 'runs' / f'{previous_job}.json', prior)
    try:
        try:
            doc = store.get_document('endurance', project)
        except FileNotFoundError:
            doc = store.save_document('endurance', VoiceDocument(
                project_id=project, video_fingerprint='synthetic-three-hour-v1',
                profile=VoiceProfile(id='ngoc-huyen', name='Ngọc Huyền', preset='Ngọc Huyền'),
                clips=[VoiceClip(id=f'clip-{i:04}', spoken_text=f'Phần {i + 1}. {TEXT}',
                                 start_ms=i * 45000, end_ms=(i + 1) * 45000, rate=1.08)
                       for i in range(240)]))
        job = manager.start('endurance', project, args.device)
        report = {'synthetic': True, 'timeline_seconds': 10800, 'device': args.device, 'job': job}
        while job['state'] in {'queued', 'running'}:
            time.sleep(5)
            job = manager.get('endurance', job['id'])
            report = {'synthetic': True, 'timeline_seconds': 10800, 'device': args.device,
                      'wall_seconds': round(time.monotonic() - started, 2), 'job': job}
            write_json(root / 'report.json', report)
            write_json(root / 'runs' / f'{job["id"]}.json', report)
            print(json.dumps({'state': job['state'], 'completed': job['completed'], 'total': job['total'] }), flush=True)
        doc = store.get_document('endurance', project)
        report['generated_audio_seconds'] = sum(c.duration_ms for c in doc.clips) / 1000
        report['attached_clips'] = sum(bool(c.asset_id) for c in doc.clips)
        report['overflow_clips'] = sum(c.status == 'overflow' for c in doc.clips)
        write_json(root / 'report.json', report)
        write_json(root / 'runs' / f'{job["id"]}.json', report)
        if job['state'] != 'succeeded':
            raise RuntimeError(job['message'])
    finally:
        manager.shutdown()


if __name__ == '__main__':
    main()
