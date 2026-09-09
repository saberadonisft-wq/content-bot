"""Export a verified contiguous prefix (or all) of the real endurance narration."""
import argparse
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.services.voiceover.mix import export_voice_audio, ffmpeg, finalize_voiced_render
from app.services.media_probe import probe_media
from app.services.voiceover.store import VoiceStore, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--clips', type=int, default=240)
    parser.add_argument('--mp4', action='store_true', help='Also mux narration into a synthetic long video')
    args = parser.parse_args()
    if not 1 <= args.clips <= 240:
        parser.error('clips must be 1..240')
    root = ROOT / 'artifacts/voiceover/endurance/cpu'
    store = VoiceStore(root)
    doc = store.get_document('endurance', 'e' * 20)
    selected = doc.clips[:args.clips]
    if len(selected) != args.clips or any(c.status != 'ready' or not c.asset_id for c in selected):
        raise RuntimeError('Requested prefix is not complete and ready; generation was not modified.')
    duration = args.clips * 45000
    started = time.monotonic()
    output = export_voice_audio(store, 'endurance', doc.model_copy(update={'clips': selected}), 'wav', {}, duration)
    with wave.open(str(output)) as audio:
        actual_seconds = audio.getnframes() / audio.getframerate()
        assert abs(actual_seconds - duration / 1000) <= .020
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
        # Read each placed line's interior and each planned trailing gap; keep memory bounded.
        import array
        checks = []
        for clip in selected:
            start = (clip.start_ms + clip.offset_ms) / 1000
            length = clip.duration_ms / clip.rate / 1000
            audio.setpos(round((start + min(.5, length / 4)) * audio.getframerate()))
            samples = array.array('h', audio.readframes(round(min(2, length / 2) * audio.getframerate())))
            assert max(map(abs, samples), default=0) > 5, f'No audio in {clip.id}'
            gap = start + length + .5
            if gap + .2 < clip.end_ms / 1000:
                audio.setpos(round(gap * audio.getframerate()))
                silence = array.array('h', audio.readframes(round(.2 * audio.getframerate())))
                assert max(map(abs, silence), default=0) <= 2, f'Unexpected audio after {clip.id}'
            checks.append(clip.id)
    report = {'clips': args.clips, 'full_run': args.clips == 240, 'timeline_seconds': actual_seconds,
              'generated_audio_seconds': sum(c.duration_ms for c in selected) / 1000,
              'wall_seconds': round(time.monotonic() - started, 2), 'output': str(output),
              'bytes': output.stat().st_size, 'checked_interiors_and_gaps': len(checks)}
    write_json(root / f'render-{args.clips}.json', report)
    print(json.dumps(report), flush=True)
    if args.mp4:
        video = root / f'fixture-{args.clips}.mp4'
        if not video.exists():
            ffmpeg(['-f', 'lavfi', '-i', f'color=c=black:s=160x90:r=1:d={duration / 1000}',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(video)])
        started_video = time.monotonic()
        result = finalize_voiced_render(store, 'endurance', doc.model_copy(update={'clips': selected}),
            {'output_filename': video.name, 'video_id': doc.project_id, 'duration_ms': duration},
            root, {'duration_ms': duration, 'has_audio': False}, {}, 1)
        final = root / result['output_filename']
        media = probe_media(final)
        assert media['has_audio'] and abs(media['duration_ms'] - duration) <= 40, media
        assert media['audio_codec'] == 'aac', media
        report['mp4'] = {'output': str(final), 'duration_ms': media['duration_ms'],
                         'bytes': final.stat().st_size, 'wall_seconds': round(time.monotonic() - started_video, 2)}
        write_json(root / f'render-{args.clips}.json', report)
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
