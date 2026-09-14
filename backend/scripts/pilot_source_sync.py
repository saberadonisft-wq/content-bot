"""Check four real source windows in an isolated copy, without TTS or project edits."""
import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.media_probe import probe_media_cached
from app.services.voiceover.models import VoiceDocument, VoiceSync
from app.services.voiceover.store import VoiceStore, read_json, write_json
from app.services.voiceover.sync_audit import audit_path
from app.services.voiceover.sync_worker import run_sync_audit_worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('subtitle_version', type=Path)
    parser.add_argument('video', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--count', type=int, default=4, choices=range(1, 5))
    parser.add_argument('--check-reuse', action='store_true')
    parser.add_argument('--whisper-model', default='small')
    parser.add_argument('--dubbed-whisper-model', default='small')
    parser.add_argument('--evaluate-dubbed', action='store_true', help='Unlock only the selected clips in the isolated copy')
    parser.add_argument('--clip-id', help='Evaluate one specific available clip instead of evenly spaced samples')
    parser.add_argument('--reuse-observations', type=Path, help='Copy existing raw ASR observations; keys are still revalidated')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    original = args.project.read_bytes()
    voice = VoiceDocument.model_validate_json(original)
    version = read_json(args.subtitle_version)
    if version['video_id'] != voice.project_id:
        raise ValueError('Subtitle version belongs to another video')
    document = version['document']
    cues = {cue['id']: cue for cue in document['segments']}
    candidates = [clip for clip in voice.clips if clip.asset_id and len(clip.source_cue_ids) == 1
        and (cue := cues.get(clip.source_cue_ids[0])) and cue.get('source_text')
        and 0 < cue['end_ms'] - cue['start_ms'] < 8000]
    if len(candidates) < args.count:
        raise ValueError('Not enough available clips with source transcript')
    selected = [candidates[round(index * (len(candidates) - 1) / max(1, args.count - 1))]
                for index in range(args.count)]
    if args.clip_id:
        selected = [clip for clip in candidates if clip.id == args.clip_id]
        if len(selected) != 1:
            raise ValueError('Selected clip is unavailable or has no source transcript')
        args.count = 1
    if args.evaluate_dubbed:
        for clip in selected:
            clip.sync = VoiceSync(timing_origin='automatic', timing_locked=False, text_locked=True)
    store, owner = VoiceStore(args.output / 'store'), 'pilot'
    if args.reuse_observations:
        target = store.owner_root(owner) / 'source-alignment' / 'observations'
        target.mkdir(parents=True, exist_ok=True)
        for observation in args.reuse_observations.glob('*.json'):
            shutil.copyfile(observation, target / observation.name)
    write_json(store.path(owner, 'projects', voice.project_id), voice.model_dump(mode='json'))
    source_assets = args.project.parent.parent / 'assets'
    checksums = {}
    for clip in selected:
        for suffix in ('.wav', '.json'):
            source = source_assets / (clip.asset_id + suffix)
            destination = store.path(owner, 'assets', clip.asset_id, suffix)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            checksums[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
    root = Path(__file__).resolve().parents[2]
    voice = store.get_document(owner, voice.project_id)
    media = probe_media_cached(args.video, root / 'data/cache/media-probes')
    started = time.perf_counter()
    identifier = 'e' * 32
    failure = None
    result = None
    try:
        result = run_sync_audit_worker(store, owner, identifier, args.video, media, voice, document,
            [clip.id for clip in selected], align_source=True, model_dir=root / 'data/models/faster-whisper',
            timeout_seconds=90, whisper_model=args.whisper_model, dubbed_whisper_model=args.dubbed_whisper_model)
    except Exception as exc:
        failure = exc
    record = read_json(audit_path(store, owner, identifier))
    report = {'wall_seconds': round(time.perf_counter() - started, 3), 'worker_result': result, 'whisper_model': args.whisper_model,
        'dubbed_whisper_model': args.dubbed_whisper_model,
        'state': record['state'], 'error': str(failure) if failure else None, 'stage': record.get('stage'),
        'revision': voice.revision, 'original_document_unchanged': args.project.read_bytes() == original,
        'original_assets_unchanged': all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == checksum
                                         for path, checksum in checksums.items()),
        'source_rows': [{'clip_id': row['clip_id'], 'issues': row['issues'],
            'source_evidence': row.get('source_evidence'), 'proposal': row.get('proposal'),
            'dubbed_audio': row.get('dubbed_audio'), 'processed': row.get('processed'),
            'completed': row.get('completed', False)} for row in record['rows']],
        'warnings': record['warnings'], 'source_observations': record.get('source_observations', []),
        'human_boundary_validation': False,
        'scope': f'{args.count} windows; CPU ASR and bounded candidate processing; no TTS or project edits'}
    write_json(args.output / 'measurement.json', report)
    if failure:
        print(json.dumps(report, ensure_ascii=True, indent=2))
        raise failure
    if args.check_reuse:
        # Change display text in the isolated input, forcing a document-cache miss.
        selected_cues = {cue_id for clip in selected for cue_id in clip.source_cue_ids}
        edited = {**document, 'segments': [{**cue, 'text': cue['text'] + ' [pilot]'}
            if cue['id'] in selected_cues else cue for cue in document['segments']]}
        repeated = time.perf_counter()
        run_sync_audit_worker(store, owner, 'f' * 32, args.video, media, voice, edited,
            [clip.id for clip in selected], align_source=True, model_dir=root / 'data/models/faster-whisper',
            timeout_seconds=90, whisper_model=args.whisper_model, dubbed_whisper_model=args.dubbed_whisper_model)
        second = read_json(audit_path(store, owner, 'f' * 32))
        report['repeat_wall_seconds'] = round(time.perf_counter() - repeated, 3)
        report['repeat_observation_cache_hits'] = sum(row['cache_hit'] for row in second['source_observations'])
        report['repeat_source_words_unchanged'] = [row['words'] for row in record['source_observations']] == [
            row['words'] for row in second['source_observations']]
        assert report['repeat_observation_cache_hits'] == args.count and report['repeat_source_words_unchanged']
    write_json(args.output / 'measurement.json', report)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
