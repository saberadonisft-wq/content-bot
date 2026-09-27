import pytest
from fastapi import HTTPException
from test_voice_sync_apply import candidate

from app.api import subtitles as api
from app.services.voiceover.store import read_json, write_json
from app.services.voiceover.sync_audit import audit_path


def test_sync_audit_status_audio_and_apply_reject_replaced_source(tmp_path, application_services, monkeypatch):
    store, doc, identifier, record, args = candidate(tmp_path)
    application_services.voiceover_manager.store = store
    media = dict(args['media'])
    monkeypatch.setattr(api, '_uploaded_video_path', lambda _: tmp_path / 'source.mp4')
    monkeypatch.setattr(api, 'probe_media_cached', lambda *a, **k: dict(media))
    registry = audit_path(store, 'user', identifier).with_suffix('.job.json')
    write_json(registry, {'job_id': None, 'project_id': doc.project_id})

    status = api.get_voice_sync_audit(identifier, {'sub': 'user'}, services=application_services)
    assert status['audit']['source_fingerprint'] == media['fingerprint']
    media['fingerprint'] = 'replacement'
    with pytest.raises(HTTPException) as error:
        api.get_voice_sync_audit(identifier, {'sub': 'user'}, services=application_services)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        api.get_voice_sync_candidate_audio(identifier, 'one', {'sub': 'user'}, services=application_services)
    assert error.value.status_code == 409

    request = api.VoiceSyncApplyRequest(video_id=doc.project_id, voice_revision=doc.revision,
                                         document=args['subtitles'], clip_ids=['one'])
    with pytest.raises(HTTPException) as error:
        api.apply_voice_sync_candidates(identifier, request, {'sub': 'user'}, services=application_services)
    assert error.value.status_code == 409
    assert read_json(audit_path(store, 'user', identifier)) == record
