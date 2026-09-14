import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_voiceover import asset, document

from app.api import subtitles as api
from app.schemas import VoiceSyncAuditRequest
from app.services.speech_evidence import SpeechEvidence, audio_identity, transcript_hash
from app.services.voiceover import sync_audit as module
from app.services.voiceover.models import VoiceSync
from app.services.voiceover.store import VoiceStore, read_json


def setup(tmp_path):
    store = VoiceStore(tmp_path / 'voice')
    doc = document()
    doc.clips[0].source_cue_ids = ['cue']
    doc.clips[0].sync = VoiceSync(timing_origin='automatic', timing_locked=False, text_locked=False)
    doc = store.save_document('user', doc)
    store.attach('user', doc.project_id, 'one', asset(store, doc))
    doc = store.get_document('user', doc.project_id)
    subtitles = {'schema_version': 2, 'language': 'vi', 'segments': [{
        'id': 'cue', 'start_ms': 1000, 'end_ms': 3000, 'text': 'Xin chào',
        'source_text': 'Hello', 'source_language': 'en', 'timing_source': 'gemini_estimate',
        'timing_precision_ms': 100, 'revision': 0}]}
    media = {'fingerprint': doc.video_fingerprint, 'has_audio': True, 'duration_ms': 5000}
    return store, doc, subtitles, media


def fake_source(monkeypatch, media):
    monkeypatch.setattr(module, 'observe_source_activity', lambda *a, **kw: {'windows': [], 'skipped': []})
    monkeypatch.setattr(module, 'inspect_dubbed_speech', lambda *a, **kw: {
        'speech_verified': False, 'issues': ['dubbed_transcript_unverified']})

    def align(video, subtitles, observed_media, **options):
        assert options['settings'].preserve_display
        assert options['settings'].whisper_device == 'cpu'
        assert not options['settings'].whisper_allow_download
        cue = deepcopy(subtitles['segments'][0])
        assert 'speech_evidence' not in cue
        cue.update(speech_start_ms=1100, speech_end_ms=1700, needs_review=False)
        cue['speech_evidence'] = SpeechEvidence(method='asr_observed', audio_identity=audio_identity(media),
            transcript_sha256=transcript_hash(cue), start_ms=1100, end_ms=1700,
            algorithm='fixture', transcript_complete=True).model_dump()
        return {'document': {**subtitles, 'segments': [cue]}, 'warnings': []}

    monkeypatch.setattr(module, 'align_subtitle_document', align)


def test_audit_records_source_and_audio_without_editing_project(tmp_path, monkeypatch):
    store, doc, subtitles, media = setup(tmp_path)
    subtitles['segments'][0]['speech_evidence'] = {'method': 'fake-import'}
    fake_source(monkeypatch, media)
    path = store.path('user', 'projects', doc.project_id)
    before = path.read_bytes()
    result = module.run_sync_audit(store, 'user', 'a' * 32, Path('unused'), media, doc, subtitles,
        ['one'], align_source=True, model_dir=tmp_path / 'models')
    record = read_json(module.audit_path(store, 'user', result['sync_audit_id']))
    assert record['state'] == 'succeeded'
    assert path.read_bytes() == before
    row = record['rows'][0]
    assert row['source_evidence']['method'] == 'asr_observed'
    assert row['audio']['duration_ms'] == 500
    assert row['proposal']['state'] == 'candidate'
    assert row['proposal']['suggested_offset_ms'] == 100
    assert row['proposal']['trim_start_ms'] == row['proposal']['borrowed_ms'] == 0
    assert not record['automatic_apply'] and not record['voice_changed_during_audit']


def test_bounds_do_not_move_next_start_and_locked_or_impossible_lines_stay_blocked(tmp_path):
    _, doc, _, _ = setup(tmp_path)
    clip = doc.clips[0]
    clip.duration_ms = 2400
    result = module.propose_source_fit(clip, 84500, 85800, 85900, allowed_tail_ms=1000)
    assert result['allowed_end_ms'] == 85900
    assert result['borrowed_ms'] == 100
    assert 'duration_not_feasible' in result['blocked_reasons']
    assert result['suggested_rate'] == 1.15
    clip.sync.timing_locked = True
    assert 'timing_locked' in module.propose_source_fit(clip, 0, 4000, None)['blocked_reasons']


def test_cancel_and_unavailable_model_leave_checkpoint_and_do_not_guess_source(tmp_path, monkeypatch):
    store, doc, subtitles, media = setup(tmp_path)
    fake_source(monkeypatch, media)
    monkeypatch.setattr(module, 'align_subtitle_document', lambda *a, **k: (_ for _ in ()).throw(ValueError('Model missing')))
    module.run_sync_audit(store, 'user', 'a' * 32, Path('unused'), media, doc, subtitles,
        ['one'], align_source=True, model_dir=tmp_path)
    record = read_json(module.audit_path(store, 'user', 'a' * 32))
    assert record['warnings'][0]['code'] == 'source_asr_unavailable'
    assert record['rows'][0]['issues'] == ['source_unverified']
    assert 'proposal' not in record['rows'][0]
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(module.SubtitleAlignmentCanceled):
        module.run_sync_audit(store, 'user', 'b' * 32, Path('unused'), media, doc, subtitles,
            ['one'], align_source=True, model_dir=tmp_path, cancel=cancel)
    assert read_json(module.audit_path(store, 'user', 'b' * 32))['state'] == 'canceled'


def test_audit_detects_edits_during_observation(tmp_path, monkeypatch):
    store, doc, subtitles, media = setup(tmp_path)
    fake_source(monkeypatch, media)
    original = module.analyze_quiet_edges

    def analyze(*args, **kwargs):
        current = store.get_document('user', doc.project_id)
        current.clips[0].offset_ms = 15
        store.save_document('user', current)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, 'analyze_quiet_edges', analyze)
    module.run_sync_audit(store, 'user', 'a' * 32, Path('unused'), media, doc, subtitles,
        ['one'], align_source=True, model_dir=tmp_path)
    record = read_json(module.audit_path(store, 'user', 'a' * 32))
    assert record['voice_changed_during_audit']


def test_dubbed_checks_have_separate_budget_and_keep_original_audio(tmp_path, monkeypatch):
    store, doc, subtitles, media = setup(tmp_path)
    doc.clips = [doc.clips[0].model_copy(deep=True, update={'id': f'clip-{i}', 'source_cue_ids': [f'cue-{i}'],
        'start_ms': 1000 + i * 1000, 'end_ms': 1800 + i * 1000}) for i in range(6)]
    subtitles['segments'] = [{**subtitles['segments'][0], 'id': f'cue-{i}',
        'start_ms': 1000 + i * 1000, 'end_ms': 1800 + i * 1000} for i in range(6)]
    media['duration_ms'] = 9000
    doc = store.save_document('user', doc)
    before = store.path('user', 'projects', doc.project_id).read_bytes()
    monkeypatch.setattr(module, 'observe_source_activity', lambda *a, **kw: {'windows': [], 'skipped': []})

    def align(path, document, *_args, **_kwargs):
        result = deepcopy(document)
        for cue in result['segments']:
            cue.update(speech_start_ms=cue['start_ms'], speech_end_ms=cue['end_ms'], needs_review=False)
            cue['speech_evidence'] = SpeechEvidence(method='asr_observed', audio_identity=audio_identity(media),
                transcript_sha256=transcript_hash(cue), start_ms=cue['start_ms'], end_ms=cue['end_ms'],
                algorithm='fixture', transcript_complete=True).model_dump()
        return {'document': result, 'warnings': []}

    monkeypatch.setattr(module, 'align_subtitle_document', align)
    monkeypatch.setattr(module, 'analyze_quiet_edges', lambda *a, **kw: {'checksum': 'a' * 64,
        'duration_ms': 500, 'protected_head_candidate_ms': 100, 'protected_tail_candidate_ms': 100})
    inspected = []

    def inspect(*args, **kwargs):
        inspected.append(args)
        return {'issues': [], 'trim_start_ms': 80, 'trim_end_ms': 80, 'speech_verified': True,
                'automatic_trim_allowed': True, 'speech_evidence': {'start_ms': 160, 'end_ms': 340}}

    monkeypatch.setattr(module, 'inspect_dubbed_speech', inspect)
    processed = []

    def prepare(*args, **kwargs):
        processed.append(args)
        return {'state': 'needs_review', 'issues': ['dubbed_transcript_unverified']}

    monkeypatch.setattr(module, 'prepare_sync_candidate', prepare)
    module.run_sync_audit(store, 'user', 'b' * 32, Path('unused'), media, doc, subtitles,
        [clip.id for clip in doc.clips], align_source=True, model_dir=tmp_path)
    record = read_json(module.audit_path(store, 'user', 'b' * 32))
    assert len(inspected) == 4
    assert record['dubbed_validation_budget'] == {'clips': 4, 'audio_ms': 2000}
    assert len(processed) == 2
    assert record['processed_validation_budget'] == {'clips': 2, 'audio_ms': 1000}
    assert all('dubbed_audio_budget' in row['issues'] for row in record['rows'][4:])
    assert all(row['proposal']['trim_start_ms'] == 80 for row in record['rows'][:4])
    assert all(not row['proposal']['output_measured'] for row in record['rows'])
    assert store.path('user', 'projects', doc.project_id).read_bytes() == before


def test_audit_api_is_owner_scoped_and_rejects_stale_revision(application_services, tmp_path, monkeypatch):
    store, doc, subtitles, media = setup(tmp_path)
    application_services.voiceover_manager.store = store
    monkeypatch.setattr(api, '_uploaded_video_path', lambda _: Path('unused'))
    monkeypatch.setattr(api, 'probe_media_cached', lambda *a, **k: media)
    fake_source(monkeypatch, media)
    monkeypatch.setattr(api, 'run_sync_audit_worker', module.run_sync_audit)
    req = VoiceSyncAuditRequest(video_id=doc.project_id, voice_revision=doc.revision,
                               document=subtitles, clip_ids=['one'])
    response = api.start_voice_sync_audit(req, {'sub': 'user'}, services=application_services)
    identifier = response['audit_id']
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = api.get_voice_sync_audit(identifier, {'sub': 'user'}, services=application_services)
        if result['job']['state'] in {'succeeded', 'failed', 'canceled'}:
            break
        time.sleep(.01)
    assert result['job']['state'] == 'succeeded', result
    assert result['audit']['rows'][0]['source_evidence']
    assert 'source_document' not in result['audit']
    with pytest.raises(HTTPException) as forbidden:
        api.get_voice_sync_audit(identifier, {'sub': 'someone-else'}, services=application_services)
    assert forbidden.value.status_code == 404
    with pytest.raises(HTTPException) as forbidden:
        api.cancel_voice_sync_audit(identifier, {'sub': 'someone-else'}, services=application_services)
    assert forbidden.value.status_code == 404
    with pytest.raises(HTTPException) as stale:
        api.start_voice_sync_audit(req.model_copy(update={'voice_revision': 0}), {'sub': 'user'}, services=application_services)
    assert stale.value.status_code == 409


def test_checkpoint_keeps_selected_rows_before_expensive_source_step(tmp_path, monkeypatch):
    store, doc, subtitles, media = setup(tmp_path)

    def stop(*args, **kwargs):
        checkpoint = read_json(module.audit_path(store, 'user', 'c' * 32))
        assert checkpoint['stage'] == 'source_activity'
        assert checkpoint['rows'][0]['clip_id'] == 'one'
        assert not checkpoint['rows'][0].get('completed')
        raise TimeoutError('pilot limit')

    monkeypatch.setattr(module, 'observe_source_activity', stop)
    with pytest.raises(TimeoutError):
        module.run_sync_audit(store, 'user', 'c' * 32, Path('unused'), media, doc, subtitles,
            ['one'], align_source=True, model_dir=tmp_path)
    assert read_json(module.audit_path(store, 'user', 'c' * 32))['state'] == 'failed'
