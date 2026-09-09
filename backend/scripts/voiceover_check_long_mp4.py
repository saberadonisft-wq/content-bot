"""Decode narration and following gaps across the completed long MP4 artifact."""
import argparse
import array
import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.services.voiceover.store import VoiceStore, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--clips', type=int, default=None, help='Number of clips in render report (e.g. 160 or 240)')
    args = parser.parse_args()

    root = ROOT / 'artifacts/voiceover/endurance/cpu'
    report = json.loads((root / 'render-160.json').read_text(encoding='utf-8'))
    if args.clips is None:
        if (root / 'render-240.json').exists():
            args.clips = 240
        elif (root / 'render-160.json').exists():
            args.clips = 160
        else:
            args.clips = 240

    report_file = root / f'render-{args.clips}.json'
    if not report_file.exists():
        raise FileNotFoundError(f'Report file not found: {report_file}')
    report = json.loads(report_file.read_text(encoding='utf-8'))
    path = Path(report['mp4']['output'])
    doc = VoiceStore(root).get_document('endurance', 'e' * 20)
    checks = []
    for index in (0, 40, 80, 120, 159):
    indices = sorted({0, args.clips // 4, args.clips // 2, (3 * args.clips) // 4, args.clips - 1})
    for index in indices:
        clip = doc.clips[index]
        start = (clip.start_ms + clip.offset_ms) / 1000
        end = start + clip.duration_ms / clip.rate / 1000
        peaks = []
        for position in (start + .5, end + .5):
            result = subprocess.run([
                imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-ss', str(position),
                '-i', str(path), '-t', '1', '-map', '0:a:0', '-ac', '1', '-ar', '48000',
                '-f', 's16le', '-',
            ], check=True, capture_output=True, timeout=30)
            samples = array.array('h', result.stdout)
            assert len(samples) >= 47000, (index, position, 'missing decoded audio')
            peaks.append(max(map(abs, samples), default=0))
        assert peaks[0] > 5, (index, 'missing speech')
        assert peaks[1] <= 5, (index, 'audio leaked into trailing gap')
        checks.append({'clip': clip.id, 'start_seconds': start, 'speech_peak': peaks[0], 'gap_peak': peaks[1]})
    write_json(root / 'mp4-decode-audit.json', {'output': str(path), 'checks': checks})
    print(json.dumps(checks), flush=True)
        checks.append({'index': index, 'clip': clip.id, 'start_seconds': start, 'speech_peak': peaks[0], 'gap_peak': peaks[1]})
    audit = {'clips': args.clips, 'output': str(path), 'checks': checks}
    write_json(root / f'mp4-decode-audit-{args.clips}.json', audit)
    write_json(root / 'mp4-decode-audit.json', audit)
    print(json.dumps(audit, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
