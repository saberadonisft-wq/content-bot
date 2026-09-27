"""Versioned, bounded source checks for dubbing. Never edits the subtitle track."""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from pathlib import Path

from ..speech_evidence import valid_speech_evidence
from ..subtitle_alignment import (
    AlignmentSettings,
    SubtitleAlignmentCanceled,
    SubtitleAlignmentError,
    align_subtitle_document,
)
from .audio import audio_metadata
from .dubbed_speech import inspect_dubbed_speech
from .fit_plan import plan_source_fit
from .models import VoiceDocument
from .quiet_analysis import analyze_quiet_edges
from .source_activity import classify_gap, observe_source_activity
from .store import (
    VoiceStore,
    digest,
    generation_hash,
    normalized_text,
    read_json,
    write_json,
)
from .sync_candidate import neighbour_bounds, prepare_sync_candidate
from .timing import current_alignment, source_signature

VERSION = 'voice-sync-audit-v1'


def audit_path(store: VoiceStore, owner: str, identifier: str) -> Path:
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('ID kiểm tra đồng bộ không hợp lệ.')
    return store.owner_root(owner) / 'sync-audits' / f'{identifier}.json'


def snapshot_binding(voice: VoiceDocument, subtitles: dict) -> str:
    # Include all editable inputs, not derived status; protect neighbouring starts too.
    return digest({'profile': voice.profile.model_dump(), 'video': voice.video_fingerprint,
                   'pronunciation': voice.pronunciation,
                   'clips': [{k: v for k, v in clip.model_dump().items() if k not in {'status', 'error'}}
                             for clip in voice.clips], 'subtitles': subtitles})


def propose_source_fit(clip, source_start: int, source_end: int, next_start: int | None,
                       *, allowed_tail_ms: int = 0) -> dict:
    """Propose whole-file fit only; no unverified silence trim or cascading ripple."""
    upper = source_end + min(250, max(0, allowed_tail_ms))
    if next_start is not None:
        upper = min(upper, next_start)
    budget = upper - source_start
    reasons = []
    if clip.sync.timing_locked or clip.sync.timing_origin != 'automatic':
        reasons.append('timing_locked')
    if not clip.asset_id or clip.duration_ms <= 0 or clip.status == 'stale':
        reasons.append('audio_not_ready')
    if budget <= 0:
        reasons.append('source_overlap')
    needed = clip.duration_ms / budget if budget > 0 else None
    rate = max(1.0, needed or 1.0)
    if rate > 1.15:
        reasons.append('duration_not_feasible')
    if clip.rate > 1.15:
        reasons.append('existing_fast_rate_needs_review')
    return {'source_start_ms': source_start, 'source_end_ms': source_end,
            'allowed_start_ms': source_start, 'allowed_end_ms': upper,
            'suggested_offset_ms': source_start - clip.start_ms,
            'suggested_rate': min(1.15, rate), 'required_rate': needed,
            'borrowed_ms': max(0, upper - source_end), 'trim_start_ms': 0, 'trim_end_ms': 0,
            'blocked_reasons': reasons, 'source_verified': False,
            'state': 'blocked' if reasons else 'candidate', 'output_measured': False}


def run_sync_audit(store: VoiceStore, owner: str, identifier: str, video: Path, media: dict,
                   voice: VoiceDocument, subtitles: dict, clip_ids: list[str], *,
                   align_source: bool, model_dir: Path, whisper_model: str = 'small',
                   dubbed_whisper_model: str = 'small',
                   cancel: threading.Event | None = None, progress=None) -> dict:
    cancel = cancel or threading.Event()
    if len(clip_ids) > 40 or len(set(clip_ids)) != len(clip_ids) or not clip_ids:
        raise ValueError('Chọn từ 1 đến 40 đoạn khác nhau cho mỗi lượt kiểm tra.')
    if voice.video_fingerprint != media.get('fingerprint'):
        raise ValueError('Video không khớp dự án giọng.')
    by_id = {clip.id: clip for clip in voice.clips}
    if any(identifier not in by_id for identifier in clip_ids):
        raise ValueError('Đoạn được chọn không thuộc dự án.')
    cue_by_id = {cue['id']: cue for cue in subtitles['segments']}
    references = Counter(cue_id for clip in voice.clips for cue_id in clip.source_cue_ids)
    path = audit_path(store, owner, identifier)
    started = time.monotonic()
    record = {'id': identifier, 'algorithm': VERSION, 'project_id': voice.project_id,
              'voice_revision': voice.revision, 'input_binding': snapshot_binding(voice, subtitles),
              'source_fingerprint': media['fingerprint'], 'state': 'running', 'clip_ids': clip_ids,
              'rows': [], 'source_document': subtitles, 'warnings': [], 'automatic_apply': False}

    def checkpoint():
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        write_json(path, record)

    def check():
        if cancel.is_set():
            raise SubtitleAlignmentCanceled('Đã hủy kiểm tra đồng bộ')
        if time.monotonic() - started > 600:
            raise TimeoutError('Kiểm tra đồng bộ vượt ngân sách 10 phút.')

    checkpoint()
    try:
        selected, windows, total = [], [], 0
        for clip_id in clip_ids:
            clip = by_id[clip_id]
            row = {'clip_id': clip_id, 'source_cue_ids': clip.source_cue_ids, 'issues': []}
            record['rows'].append(row)
            if (len(clip.source_cue_ids) != 1 or clip.source_cue_ids[0] not in cue_by_id
                    or references[clip.source_cue_ids[0]] != 1):
                row['issues'].append('source_mapping_unresolved')
                continue
            cue = cue_by_id[clip.source_cue_ids[0]]
            alignment = current_alignment(clip)
            if alignment and clip.sync.state == 'aligned' and alignment.source_signature == source_signature(cue):
                meta = read_json(store.path(owner, 'assets', clip.asset_id))
                if (generation_hash(voice, clip, meta['device']) == meta['generation_hash']
                        and audio_metadata(store.path(owner, 'assets', clip.asset_id, '.wav'))['checksum'] == meta['checksum']):
                    row.update(already_aligned=True, completed=True, alignment=alignment.model_dump())
                    continue
            lower = max(0, cue['start_ms'] - 650)
            upper = min(media['duration_ms'], cue['end_ms'] + 650)
            if lower >= upper or upper - lower > 30_000 or total + upper - lower > 180_000:
                row['issues'].append('source_audio_budget')
                continue
            total += upper - lower
            selected.append(cue)
            windows.append((lower, upper))
        record['stage'] = 'source_activity'
        checkpoint()
        check()
        if progress:
            progress(5, 'source', 'Đang khoanh vùng lời nói nguồn')
        observations = observe_source_activity(video, media, windows,
            cache_dir=store.owner_root(owner) / 'source-activity', cancel=cancel) if windows else {'windows': [], 'skipped': []}
        record['activity'] = observations
        record['stage'] = 'source_alignment'
        checkpoint()
        check()
        aligned = {}
        if align_source and selected:
            # Caller-provided evidence is not trusted. Always derive or reuse our own cache.
            source_input = {**subtitles, 'segments': [{k: v for k, v in cue.items() if k != 'speech_evidence'}
                                                    for cue in selected]}
            candidates = {cue['id'] for cue in selected if cue.get('source_text') or cue.get('secondary_text')}
            if candidates:
                try:
                    result = align_subtitle_document(video, source_input, media,
                        settings=AlignmentSettings(engine='faster_whisper', preserve_display=True,
                            whisper_device='cpu', whisper_compute_type='int8', cpu_threads=3,
                            whisper_model=whisper_model, whisper_model_dir=model_dir, whisper_allow_download=False),
                        cue_ids=candidates, cache_dir=store.owner_root(owner) / 'source-alignment',
                        cancel_event=cancel, progress=progress)
                    aligned = {cue['id']: cue for cue in result['document']['segments']}
                    record['warnings'].extend(result['warnings'])
                    record['source_observations'] = result.get('source_observations', [])
                except SubtitleAlignmentCanceled:
                    raise
                except (SubtitleAlignmentError, OSError, ValueError, RuntimeError) as exc:
                    record['warnings'].append({'code': 'source_asr_unavailable', 'message': str(exc)[:500]})
        record['stage'] = 'dubbed_audio'
        checkpoint()
        dubbed_audio_ms = 0
        dubbed_checks = 0
        processed_checks = 0
        processed_audio_ms = 0
        for index, row in enumerate(record['rows']):
            check()
            if row.get('already_aligned'):
                continue
            clip = by_id[row['clip_id']]
            if clip.asset_id:
                meta = read_json(store.path(owner, 'assets', clip.asset_id))
                if generation_hash(voice, clip, meta['device']) != meta['generation_hash']:
                    row['issues'].append('audio_stale')
                else:
                    row['audio'] = analyze_quiet_edges(store.path(owner, 'assets', clip.asset_id, '.wav'),
                        expected_checksum=meta['checksum'], cache_dir=store.owner_root(owner) / 'quiet-analysis', cancel=cancel)
            cue = aligned.get(clip.source_cue_ids[0]) if len(clip.source_cue_ids) == 1 else None
            evidence = valid_speech_evidence(cue, media) if cue else None
            if evidence and evidence.method == 'asr_observed' and evidence.transcript_complete and not cue.get('needs_review'):
                row['source_evidence'] = evidence.model_dump()
                quiet = row.get('audio')
                if quiet and clip.sync.timing_origin == 'automatic' and not clip.sync.timing_locked:
                    if dubbed_checks >= 4 or dubbed_audio_ms + quiet['duration_ms'] > 60_000:
                        row['issues'].append('dubbed_audio_budget')
                    else:
                        dubbed_checks += 1
                        dubbed_audio_ms += quiet['duration_ms']
                        record['dubbed_validation_budget'] = {'clips': dubbed_checks, 'audio_ms': dubbed_audio_ms}
                        checkpoint()
                        check()
                        row['dubbed_audio'] = inspect_dubbed_speech(
                            store.path(owner, 'assets', clip.asset_id, '.wav'), normalized_text(clip.spoken_text, voice.pronunciation, voice.text_normalization),
                            checksum=quiet['checksum'], model_dir=model_dir, whisper_model=dubbed_whisper_model,
                            cache_dir=store.owner_root(owner) / 'dubbed-speech', cancel=cancel)
                        row['issues'].extend(row['dubbed_audio']['issues'])
                # Conservative bound: keep every other utterance's current onset fixed.
                previous_end, next_start = neighbour_bounds(voice, clip, evidence.start_ms, media['duration_ms'])
                measured_clip = clip.model_copy(update={'duration_ms': row['audio']['duration_ms']}) if row.get('audio') else clip
                row['proposal'] = propose_source_fit(measured_clip, evidence.start_ms, evidence.end_ms, next_start)
                if row.get('dubbed_audio', {}).get('speech_verified'):
                    row['proposal'] = plan_source_fit(measured_clip, evidence.start_ms, evidence.end_ms,
                        video_duration_ms=media['duration_ms'], previous_file_end_ms=previous_end,
                        next_file_start_ms=next_start, audio=row['dubbed_audio'])
                if 'audio_stale' in row['issues']:
                    row['proposal']['blocked_reasons'].append('audio_stale')
                    row['proposal']['state'] = 'blocked'
                if row['proposal']['state'] == 'candidate' and row['proposal'].get('dubbed_speech_verified'):
                    # Separate output recheck budget, still inside the worker's hard deadline.
                    if processed_checks >= 2 or processed_audio_ms + quiet['duration_ms'] > 30_000:
                        row['issues'].append('processed_audio_budget')
                    else:
                        processed_checks += 1
                        processed_audio_ms += quiet['duration_ms']
                        record['processed_validation_budget'] = {'clips': processed_checks, 'audio_ms': processed_audio_ms}
                        checkpoint()
                        check()
                        try:
                            row['processed'] = prepare_sync_candidate(voice, measured_clip, row['proposal'],
                                source=store.path(owner, 'assets', clip.asset_id, '.wav'), checksum=quiet['checksum'],
                                video_duration_ms=media['duration_ms'], cache_dir=store.owner_root(owner) / 'sync-candidates',
                                model_dir=model_dir, whisper_model=dubbed_whisper_model, cancel=cancel)
                        except (SubtitleAlignmentError, OSError, ValueError, RuntimeError) as exc:
                            check()
                            row['issues'].append('processed_audio_failed')
                            row['processing_error'] = str(exc)[:500]
            else:
                row['issues'].append('source_unverified')
            if len(clip.source_cue_ids) == 1:
                source_id = clip.source_cue_ids[0]
                row['source_diagnostics'] = [warning['diagnostics'] for warning in record['warnings']
                    if warning.get('cue_id') == source_id and warning.get('diagnostics')]
                source_cue = cue_by_id.get(source_id)
                if source_cue:
                    next_cue = min((other for other in subtitles['segments']
                                   if other['start_ms'] >= source_cue['end_ms'] and other['id'] != source_id),
                                  key=lambda other: other['start_ms'], default=None)
                    covered_end = any(other['id'] != source_id
                        and other['start_ms'] < source_cue['end_ms'] < other['end_ms'] for other in subtitles['segments'])
                    if not covered_end and next_cue and next_cue['start_ms'] > source_cue['end_ms']:
                        row['following_gap'] = {'start_ms': source_cue['end_ms'], 'end_ms': next_cue['start_ms'],
                            'classification': classify_gap(source_cue['end_ms'], next_cue['start_ms'], observations),
                            'automatic_borrow_allowed': False}
            row['completed'] = True
            checkpoint()
            if progress:
                progress(60 + round((index + 1) / len(record['rows']) * 35), 'audio',
                         f"Đã kiểm tra {index + 1}/{len(record['rows'])} đoạn giọng")
        check()
        current = store.get_document(owner, voice.project_id)
        record['voice_changed_during_audit'] = snapshot_binding(current, subtitles) != record['input_binding']
        record['state'] = 'succeeded'
        checkpoint()
        return {'sync_audit_id': identifier, 'project_id': voice.project_id, 'rows': len(record['rows'])}
    except BaseException:
        record['state'] = 'canceled' if cancel.is_set() else 'failed'
        checkpoint()
        raise
