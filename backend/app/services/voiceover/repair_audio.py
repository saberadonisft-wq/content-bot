"""Measure staged repair audio with the same source-fit and processed-WAV checks."""
from __future__ import annotations

import json

from .audio import audio_metadata
from .dubbed_speech import inspect_dubbed_speech
from .fit_plan import plan_source_fit
from .models import VoiceClip
from .repair_review import candidate_id, review_passes
from .repair_text import validate_alternatives
from .source_pause import verified_tail
from .store import generation_hash, normalized_text, read_json, write_json
from .sync_audit import audit_path, snapshot_binding
from .sync_candidate import neighbour_bounds, prepare_sync_candidate


def validate_repair_asset(store, owner, voice, clip, subtitles, repair):
    """Recheck server-bound wording and both immutable input WAVs, including at apply."""
    if (clip.sync.text_locked or clip.sync.timing_locked or clip.sync.timing_origin != 'automatic'
            or repair['clip_id'] != clip.id or repair['original_asset_id'] != clip.asset_id):
        raise ValueError('Đầu vào hoặc khóa đoạn đã thay đổi; không áp bản sửa.')
    if clip.asset_id:
        original = read_json(store.path(owner, 'assets', clip.asset_id))
        if (audio_metadata(store.path(owner, 'assets', clip.asset_id, '.wav'))['checksum'] != repair['original_checksum']
                or original['checksum'] != repair['original_checksum']
                or generation_hash(voice, clip, original['device']) != original['generation_hash']):
            raise ValueError('WAV hoặc lời đọc trước sửa đã thay đổi.')
    elif repair['original_checksum'] is not None or repair['mode'] != 'retry_original':
        raise ValueError('Đoạn thiếu WAV chỉ được tạo lại nguyên văn.')
    text = repair['spoken_text']
    if repair['mode'] == 'retry_original':
        if text != clip.spoken_text:
            raise ValueError('Lượt sinh lại nguyên văn không được đổi chữ.')
    elif repair['mode'] == 'shorten':
        source = next(row for row in subtitles['segments'] if row['id'] == clip.source_cue_ids[0])
        context = repair['input']
        if (context['id'] != clip.id or context['spoken_text'] != clip.spoken_text
                or context['source_text'] != (source.get('source_text') or source.get('secondary_text') or '')
                or context['display_text'] != source['text']
                or repair['candidate_id'] != candidate_id(context, text)
                or repair['semantic_review']['candidate_id'] != repair['candidate_id']
                or not review_passes(repair['semantic_review'])):
            raise ValueError('Bản rút gọn chưa đạt đối chiếu nội dung hiện tại.')
        raw = json.dumps({'rows': [{'id': clip.id, 'alternatives': [{'spoken_text': text,
            **{key: repair['semantic_review'][key] for key in ('meaning_preserved', 'polarity_preserved',
                    'quantities_preserved', 'addressing_preserved', 'tone_preserved')}}]}]})
        if validate_alternatives(raw, [context])[clip.id] != [text]:
            raise ValueError('Bản rút gọn không qua bảo vệ nghĩa/số/xưng hô.')
    else:
        raise ValueError('Loại sửa giọng không hợp lệ.')
    proposed = VoiceClip.model_validate({**clip.model_dump(), 'spoken_text': text,
                                         'asset_id': repair['asset']['id'], 'status': 'ready'})
    metadata = read_json(store.path(owner, 'assets', proposed.asset_id))
    path = store.path(owner, 'assets', proposed.asset_id, '.wav')
    measured = audio_metadata(path)
    if (metadata['checksum'] != repair['asset']['checksum'] or measured['checksum'] != metadata['checksum']
            or generation_hash(voice, proposed, metadata['device']) != metadata['generation_hash']):
        raise ValueError('WAV ứng viên không khớp lời đọc đã duyệt.')
    proposed.duration_ms, proposed.generation_hash = measured['duration_ms'], metadata['generation_hash']
    return proposed, metadata, path


def run_repair_audio(store, *, owner, identifier, media, voice, subtitles, clip_ids, model_dir,
                     dubbed_whisper_model, repair, progress=None, **unused):
    """Runs only in the supervised CPU process; original project is read-only."""
    row = repair['row']
    attempt = repair.get('attempt')
    if clip_ids != [row['clip_id']] or (attempt and attempt['clip_id'] != row['clip_id']):
        raise ValueError('Ứng viên không khớp cụm cần kiểm tra.')
    original = next(clip for clip in voice.clips if clip.id == row['clip_id'])
    if repair.get('kind') == 'borrow':
        if original.sync.timing_locked or original.sync.timing_origin != 'automatic':
            raise ValueError('Mốc đã khóa; không mượn khoảng nghỉ.')
        proposed = original.model_copy(deep=True)
        metadata = read_json(store.path(owner, 'assets', original.asset_id))
        path = store.path(owner, 'assets', original.asset_id, '.wav')
        if generation_hash(voice, original, metadata['device']) != metadata['generation_hash']:
            raise ValueError('WAV không còn khớp lời đọc.')
    else:
        proposed, metadata, path = validate_repair_asset(store, owner, voice, original, subtitles, attempt)
    source = row['source_evidence']
    if source.get('method') != 'asr_observed' or not source.get('transcript_complete'):
        raise ValueError('Chưa có vùng nói nguồn đủ bằng chứng.')
    binding = snapshot_binding(voice, subtitles)
    staged = voice.model_copy(deep=True)
    staged.clips = [proposed if clip.id == proposed.id else clip for clip in staged.clips]
    result_row = {'clip_id': original.id, 'source_cue_ids': original.source_cue_ids,
                  'source_evidence': source, 'issues': [], 'completed': False}
    if attempt:
        result_row['repair'] = attempt
    record = {'id': identifier, 'project_id': voice.project_id, 'voice_revision': voice.revision,
              'source_fingerprint': media['fingerprint'], 'source_document': subtitles,
              'input_binding': binding, 'state': 'running', 'rows': [result_row],
              'automatic_apply': False, 'algorithm': 'voice-repair-audio-v1'}
    target = audit_path(store, owner, identifier)
    write_json(target, record)
    if progress:
        progress(15, 'repair_audio', 'Đang kiểm tra lời đọc vừa tạo')
    inspected = inspect_dubbed_speech(path, normalized_text(proposed.spoken_text, voice.pronunciation),
        checksum=metadata['checksum'], model_dir=model_dir, whisper_model=dubbed_whisper_model,
        cache_dir=store.owner_root(owner) / 'dubbed-speech')
    result_row.update(dubbed_audio=inspected, issues=inspected['issues'])
    write_json(target, record)
    if inspected.get('speech_verified'):
        previous, following = neighbour_bounds(voice, original, source['start_ms'], media['duration_ms'])
        tail = 0
        if repair.get('kind') == 'borrow':
            result_row['pause_evidence'] = verified_tail(unused['video'], media, source['end_ms'], following,
                repair['context'], cache_dir=store.owner_root(owner) / 'source-pause')
            tail = result_row['pause_evidence']['allowed_ms']
        result_row['proposal'] = plan_source_fit(proposed, source['start_ms'], source['end_ms'],
            video_duration_ms=media['duration_ms'], previous_file_end_ms=previous,
            next_file_start_ms=following, audio=inspected, verified_tail_ms=tail)
        write_json(target, record)
        if result_row['proposal']['state'] == 'candidate':
            if progress:
                progress(60, 'repair_audio', 'Đang đo lại WAV sau căn thời gian')
            result_row['processed'] = prepare_sync_candidate(staged, proposed, result_row['proposal'],
                source=path, checksum=metadata['checksum'], video_duration_ms=media['duration_ms'],
                cache_dir=store.owner_root(owner) / 'sync-candidates', model_dir=model_dir,
                whisper_model=dubbed_whisper_model)
    result_row['completed'] = True
    record['state'] = 'succeeded'
    write_json(target, record)
    return {'sync_audit_id': identifier, 'project_id': voice.project_id, 'rows': 1}
