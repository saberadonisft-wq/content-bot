"""Read-only audit of completed endurance assets while generation may continue."""
import json
import sys
import wave
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
import itertools

from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.store import (
    VoiceStore,
    generation_hash,
    read_json,
    write_json,
)


def main():
    root = ROOT / 'artifacts/voiceover/endurance/cpu'
    store = VoiceStore(root)
    doc = store.get_document('endurance', 'e' * 20)
    failures, measured, bounds = [], {}, []
    for clip in doc.clips:
        if not clip.asset_id:
            continue
        try:
            meta = read_json(store.path('endurance', 'assets', clip.asset_id))
            actual = measured.get(clip.asset_id)
            if actual is None:
                actual = audio_metadata(store.path('endurance', 'assets', clip.asset_id, '.wav'))
                measured[clip.asset_id] = actual
            assert actual['checksum'] == meta['checksum'], 'checksum mismatch'
            assert generation_hash(doc, clip, meta['device']) == meta['generation_hash'], 'generation mismatch'
            assert abs(actual['duration_ms'] - clip.duration_ms) <= 1, 'duration mismatch'
            start = clip.start_ms + clip.offset_ms
            end = start + actual['duration_ms'] / clip.rate
            assert end <= clip.end_ms + clip.offset_ms + 2, 'speech exceeds cue window'
            bounds.append((start, end, clip.id))
        except (AssertionError, ValueError, OSError, KeyError, wave.Error, EOFError) as error:
            failures.append({'clip_id': clip.id, 'error': str(error)})
    bounds.sort()
    for previous, following in itertools.pairwise(bounds):
        if following[0] < previous[1] - 2:
            failures.append({'clip_id': following[2], 'error': f'overlap with {previous[2]}'})
    report = {
        'document_revision': doc.revision, 'planned_clips': len(doc.clips),
        'attached_clips': sum(bool(c.asset_id) for c in doc.clips),
        'unique_verified_assets': len(measured),
        'generated_audio_seconds': sum(c.duration_ms for c in doc.clips if c.asset_id) / 1000,
        'placed_speech_seconds': sum(c.duration_ms / c.rate for c in doc.clips if c.asset_id) / 1000,
        'status_counts': dict(Counter(c.status for c in doc.clips)), 'failures': failures,
        'complete': all(c.asset_id for c in doc.clips) and not failures,
    }
    write_json(root / 'asset-audit.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
