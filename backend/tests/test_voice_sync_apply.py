import wave
from copy import deepcopy

import pytest
from test_voice_sync_audit import setup

from app.services.voiceover import mix
from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.audio_cache import AudioCache
from app.services.voiceover.models import VoiceDocument
from app.services.voiceover.processed_audio import prepare_audio
from app.services.voiceover.store import read_json, write_json
from app.services.voiceover.sync_apply import apply_sync_candidates
from app.services.voiceover.sync_audit import audit_path, snapshot_binding


def candidate(tmp_path):
    store, doc, subtitles, media = setup(tmp_path)
    clip = doc.clips[0]
    source = store.path('user', 'assets', clip.asset_id, '.wav')
    prepared = prepare_audio(source, checksum=audio_metadata(source)['checksum'], trim_start_ms=50,
        trim_end_ms=50, rate=1.1, cache_dir=store.owner_root('user') / 'sync-candidates' / 'audio')
    row = {'clip_id': clip.id, 'completed': True, 'source_evidence': {'start_ms': 900, 'end_ms': 1250,
            'method': 'asr_observed', 'transcript_complete': True},
        'proposal': {'allowed_end_ms': 1250}, 'processed': {'state': 'ready_for_review', 'issues': [],
            'output_speech_verified': True, 'offset_ms': -175, 'audio': prepared,
            'inspection': {'speech_verified': True, 'speech_evidence': {'start_ms': 75, 'end_ms': 290}}}}
    identifier = 'a' * 32
    record = {'id': identifier, 'state': 'succeeded', 'project_id': doc.project_id,
        'source_fingerprint': media['fingerprint'], 'input_binding': snapshot_binding(doc, subtitles), 'rows': [row]}
    write_json(audit_path(store, 'user', identifier), record)
    arguments = {'project_id': doc.project_id, 'revision': doc.revision, 'subtitles': subtitles,
                 'clip_ids': ['one'], 'media': media}
    return store, doc, identifier, record, arguments


def test_apply_reload_undo_and_export_share_the_processed_waveform(tmp_path):
    store, before, identifier, record, args = candidate(tmp_path)
    project_path = store.path('user', 'projects', before.project_id)
    snapshot = project_path.read_bytes()
    original = store.path('user', 'assets', before.clips[0].asset_id, '.wav')
    original_bytes = original.read_bytes()
    applied = apply_sync_candidates(store, 'user', identifier, **args)
    clip = applied.clips[0]
    assert clip.rate == 1 and clip.offset_ms == -175
    assert (clip.start_ms, clip.end_ms, clip.spoken_text) == (1000, 3000, before.clips[0].spoken_text)
    assert clip.sync.state == 'aligned' and clip.sync.issues == []
    assert clip.sync.alignment.original_asset_id == before.clips[0].asset_id
    assert store.get_document('user', before.project_id).model_dump() == applied.model_dump()
    repeated = apply_sync_candidates(store, 'user', identifier, **{**args, 'revision': applied.revision})
    assert repeated.revision == applied.revision and repeated.clips[0].offset_ms == -175
    prepared = record['rows'][0]['processed']['audio']
    cached = store.owner_root('user') / 'sync-candidates' / 'audio' / prepared['filename']
    active = store.path('user', 'assets', clip.asset_id, '.wav')
    assert cached.read_bytes() == active.read_bytes()
    # Real mixing must keep identical PCM, with neither another tempo pass nor an export-only fade.
    output = mix.compose_voice(store, 'user', applied, 5000)
    with wave.open(str(active)) as wav:
        count, expected = wav.getnframes(), wav.readframes(wav.getnframes())
    with wave.open(str(output)) as wav:
        wav.setpos(825 * 48)
        assert wav.readframes(count) == expected
    # Public audio export maps source time into the retained segment at 1x.
    AudioCache(cached.parent, max_bytes=1).trim()
    assert not cached.exists() and active.exists()  # Applied assets survive cache eviction.
    output = mix.export_voice_audio(store, 'user', applied, 'wav', {'trim_start_ms': 500}, 5000)
    with wave.open(str(output)) as wav:
        wav.setpos(325 * 48)
        assert wav.readframes(count) == expected
    assert original.read_bytes() == original_bytes
    assert any(path.read_bytes() == snapshot for path in project_path.parent.glob('snapshots/**/*.before-sync.json'))
    restored = VoiceDocument.model_validate_json(snapshot)
    restored.revision = applied.revision
    restored = store.save_document('user', restored)
    assert restored.clips[0].asset_id == before.clips[0].asset_id and restored.clips[0].offset_ms == 0


@pytest.mark.parametrize('change', ['voice', 'subtitles', 'wave', 'candidate', 'lock', 'blocked'])
def test_changed_inputs_and_unverified_output_never_apply(tmp_path, change):
    store, doc, identifier, record, args = candidate(tmp_path)
    if change in {'voice', 'lock'}:
        if change == 'voice':
            doc.clips[0].spoken_text += ' edited'
        else:
            doc.clips[0].sync.timing_locked = True
        doc = store.save_document('user', doc)
        args['revision'] = doc.revision
    elif change == 'subtitles':
        args['subtitles']['segments'][0]['source_text'] += ' edited'
    elif change == 'wave':
        store.path('user', 'assets', doc.clips[0].asset_id, '.wav').write_bytes(b'broken')
    elif change == 'candidate':
        prepared = record['rows'][0]['processed']['audio']
        (store.owner_root('user') / 'sync-candidates' / 'audio' / prepared['filename']).write_bytes(b'broken')
    else:
        record['rows'][0]['processed']['state'] = 'needs_review'
        write_json(audit_path(store, 'user', identifier), record)
    before = store.path('user', 'projects', doc.project_id).read_bytes()
    with pytest.raises((ValueError, EOFError, wave.Error)):
        apply_sync_candidates(store, 'user', identifier, **args)
    assert store.path('user', 'projects', doc.project_id).read_bytes() == before


def test_client_cannot_forge_source_window_and_edits_invalidate_proof(tmp_path):
    store, doc, identifier, _, args = candidate(tmp_path)
    applied = apply_sync_candidates(store, 'user', identifier, **args)
    tampered = applied.model_copy(deep=True)
    tampered.clips[0].sync.alignment.allowed_end_ms = 4000
    saved = store.save_document('user', tampered)
    assert saved.clips[0].sync.alignment is None and 'source_unverified' in saved.clips[0].sync.issues
    # Original server proof can be restored by undo, but moving the clip invalidates it.
    restored = deepcopy(applied)
    restored.revision = saved.revision
    restored.clips[0].offset_ms += 100
    saved = store.save_document('user', restored)
    assert saved.clips[0].sync.alignment is None
    assert read_json(store.path('user', 'projects', doc.project_id))['clips'][0]['offset_ms'] == -75


def test_combined_candidates_cannot_overlap_even_if_each_was_prepared_separately(tmp_path):
    store, doc, identifier, record, args = candidate(tmp_path)
    doc.clips.append(doc.clips[0].model_copy(deep=True, update={
        'id': 'two', 'source_cue_ids': ['cue2'], 'start_ms': 2000, 'end_ms': 3000}))
    doc = store.save_document('user', doc)
    args['subtitles']['segments'].append({**args['subtitles']['segments'][0], 'id': 'cue2', 'start_ms': 2000})
    args.update(revision=doc.revision, clip_ids=['one', 'two'])
    second = deepcopy(record['rows'][0])
    second['clip_id'] = 'two'
    second['source_evidence'].update(start_ms=950, end_ms=1300)
    second['proposal']['allowed_end_ms'] = 1300
    second['processed']['offset_ms'] = -1125
    record['rows'].append(second)
    record['input_binding'] = snapshot_binding(doc, args['subtitles'])
    write_json(audit_path(store, 'user', identifier), record)
    before = store.path('user', 'projects', doc.project_id).read_bytes()
    with pytest.raises(ValueError, match='kết hợp'):
        apply_sync_candidates(store, 'user', identifier, **args)
    assert store.path('user', 'projects', doc.project_id).read_bytes() == before


def test_aligned_unchanged_clip_is_kept_without_loading_models(tmp_path, monkeypatch):
    from app.services.voiceover import sync_audit

    store, _, identifier, _, args = candidate(tmp_path)
    applied = apply_sync_candidates(store, 'user', identifier, **args)

    def forbidden(*_args, **_kwargs):
        pytest.fail('Aligned unchanged audio must not rerun VAD/ASR/FFmpeg')

    for name in ['observe_source_activity', 'align_subtitle_document', 'inspect_dubbed_speech', 'prepare_sync_candidate']:
        monkeypatch.setattr(sync_audit, name, forbidden)
    sync_audit.run_sync_audit(store, 'user', 'c' * 32, tmp_path / 'unused', args['media'], applied,
        args['subtitles'], ['one'], align_source=True, model_dir=tmp_path)
    result = read_json(audit_path(store, 'user', 'c' * 32))
    assert result['rows'][0]['already_aligned'] and result['state'] == 'succeeded'


def test_apply_api_enforces_owner_and_passes_validated_source_document(application_services, tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.api import subtitles as api
    from app.schemas import VoiceSyncApplyRequest

    store, doc, identifier, record, args = candidate(tmp_path)
    application_services.voiceover_manager.store = store
    monkeypatch.setattr(api, '_uploaded_video_path', lambda _: tmp_path / 'unused')
    monkeypatch.setattr(api, 'probe_media_cached', lambda *a, **kw: args['media'])
    req = VoiceSyncApplyRequest(video_id=doc.project_id, voice_revision=doc.revision,
        document=args['subtitles'], clip_ids=['one'])
    record['input_binding'] = snapshot_binding(doc, req.document.model_dump(mode='json'))
    write_json(audit_path(store, 'user', identifier), record)
    with pytest.raises(HTTPException) as forbidden:
        api.apply_voice_sync_candidates(identifier, req, {'sub': 'another-owner'}, services=application_services)
    assert forbidden.value.status_code == 404
    applied = api.apply_voice_sync_candidates(identifier, req, {'sub': 'user'}, services=application_services)
    assert applied.clips[0].sync.state == 'aligned'
    from app.schemas import SubtitleRenderRequestV2

    changed_source = deepcopy(req.document.model_dump(mode='json'))
    changed_source['segments'][0]['source_text'] = 'Changed source'
    render = SubtitleRenderRequestV2(video_id=doc.project_id, voice_project_id=doc.project_id,
        voice_revision=applied.revision, document=changed_source)
    with pytest.raises(HTTPException) as changed:
        api.render_subtitle_timeline_v2_endpoint(render, {'sub': 'user'}, services=application_services)
    assert changed.value.status_code == 422 and 'Phụ đề nguồn đã đổi' in changed.value.detail
    with pytest.raises(HTTPException) as stale:
        api.apply_voice_sync_candidates(identifier, req, {'sub': 'user'}, services=application_services)
    assert stale.value.status_code == 409
