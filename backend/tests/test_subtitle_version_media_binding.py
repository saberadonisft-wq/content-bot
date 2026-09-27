from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_subtitle_jobs import _wait_for_state
from test_subtitle_versions import document

from app.api import subtitles as api
from app.main import app
from app.schemas import GeminiSubtitleRequest, SubtitleAsrRequest, SubtitleOcrRequest
from app.services.subtitle_versions import SubtitleVersionStore


@pytest.fixture
def binding(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'fixture media; probe is mocked')
    media = {'fingerprint': 'original', 'duration_ms': 2000, 'has_audio': True}
    monkeypatch.setattr(api.settings, 'content_bot_data_dir', tmp_path)
    monkeypatch.setattr(api, '_uploaded_video_path', lambda _: video)
    monkeypatch.setattr(api, 'probe_media_cached', lambda *a, **k: dict(media))
    return media, SubtitleVersionStore(tmp_path / 'subtitle-versions')


def test_manual_save_and_read_are_bound_to_current_video(binding):
    media, store = binding
    video_id = 'a' * 12
    with TestClient(app) as client:
        saved = client.post(f'/api/v1/subtitles/videos/{video_id}/versions', json={'document': document()})
        assert saved.status_code == 200, saved.text
        record = saved.json()
        assert record['version'] == 2 and record['media_binding'] == 'match'
        assert record['document']['media_fingerprint'] == 'original'
        assert store.load(video_id, record['id'])['media_fingerprint'] == 'original'
        endpoint = f"/api/v1/subtitles/videos/{video_id}/versions/{record['id']}"
        assert client.get(endpoint).json()['media_binding'] == 'match'
        media['fingerprint'] = 'replacement'
        assert client.get(endpoint).status_code == 409
        history = client.get(f'/api/v1/subtitles/videos/{video_id}/versions').json()
        assert history['versions'][0]['media_binding'] == 'mismatch'
        stale_save = client.post(f'/api/v1/subtitles/videos/{video_id}/versions', json={'document': record['document']})
        assert stale_save.status_code == 409
        assert store.list(video_id)['total'] == 1


def test_legacy_version_remains_unverified_and_immutable(binding):
    _, store = binding
    record = store.save('a' * 12, document())
    with TestClient(app) as client:
        loaded = client.get(f"/api/v1/subtitles/videos/{'a' * 12}/versions/{record['id']}")
        assert loaded.status_code == 200
        assert loaded.json()['media_binding'] == 'unverified'
        assert not loaded.json()['document'].get('media_fingerprint')
    assert store.load('a' * 12, record['id']) == record


def test_backup_preserves_old_binding_to_allow_fresh_extraction(binding):
    _, store = binding
    with TestClient(app) as client:
        response = client.post(f"/api/v1/subtitles/videos/{'a' * 12}/versions", json={
            'document': {**document(), 'media_fingerprint': 'old-video'}, 'preserve_source': True})
        assert response.status_code == 200
        saved = response.json()
        assert saved['media_binding'] == 'mismatch'
        assert store.load('a' * 12, saved['id'])['document']['media_fingerprint'] == 'old-video'


@pytest.mark.parametrize('engine', ['ocr', 'asr'])
def test_extraction_returns_bound_document_and_version(binding, monkeypatch, application_services, engine):
    _, store = binding
    monkeypatch.setattr(api, f'extract_subtitles_{engine}', lambda *a, **k: {'document': document()})
    request = SubtitleOcrRequest(video_id='a' * 12) if engine == 'ocr' else SubtitleAsrRequest(video_id='a' * 12)
    endpoint = api.extract_subtitles_ocr_endpoint if engine == 'ocr' else api.extract_subtitles_asr_endpoint
    job = endpoint(request, services=application_services)
    result = _wait_for_state(application_services.subtitle_jobs, job['id'], {'succeeded'})['result']
    assert result['document']['media_fingerprint'] == 'original'
    assert store.load('a' * 12, result['version_id'])['document'] == result['document']


@pytest.mark.parametrize('partial', [False, True])
def test_translation_does_not_save_after_video_replacement(binding, monkeypatch, partial):
    from app.services.subtitle_translate import SubtitleTranslateError

    media, store = binding

    def translate(service, doc, **kwargs):
        media['fingerprint'] = 'replacement'
        result = {'document': doc.model_dump(), 'translated_count': 1, 'total_count': 2}
        if partial:
            raise SubtitleTranslateError('fixture partial', partial_result=result)
        return result

    monkeypatch.setattr(api, 'translate_source_document', translate)
    with TestClient(app) as client:
        response = client.post('/api/v1/subtitles/v2/translate/gemini', json={
            'video_id': 'a' * 12, 'document': {**document(), 'media_fingerprint': 'original'}})
        assert response.status_code == 200
        job = _wait_for_state(app.state.services.subtitle_jobs, response.json()['id'], {'failed'})
        assert 'thay đổi' in job['error'] and not job['result']
        assert store.list('a' * 12)['total'] == 0


@pytest.mark.parametrize('endpoint', ['translate/gemini', 'render', 'align'])
def test_stale_document_is_rejected_before_work(binding, endpoint):
    # All three paths must reject before calling a model or renderer.
    with TestClient(app) as client:
        response = client.post(f'/api/v1/subtitles/v2/{endpoint}', json={
            'video_id': 'a' * 12, 'document': {**document(), 'media_fingerprint': 'different'}})
        assert response.status_code == 409, response.text


def test_generation_jobs_and_recipes_are_scoped_to_video_id(binding, monkeypatch, application_services):
    _, store = binding
    monkeypatch.setattr(application_services, 'gemini_subtitle_service', SimpleNamespace(
        cache_policy=lambda: {'version': 1}, generate=lambda *a: {'document': document()}))
    jobs = [api.generate_subtitles_with_gemini_endpoint(GeminiSubtitleRequest(video_id=video_id),
                                                      services=application_services)
            for video_id in ('a' * 12, 'b' * 12)]
    assert jobs[0]['id'] != jobs[1]['id']
    for video_id, job in zip(('a' * 12, 'b' * 12), jobs, strict=True):
        result = _wait_for_state(application_services.gemini_subtitle_jobs, job['id'], {'succeeded'})['result']
        record = store.load(video_id, result['version_id'])
        assert record['video_id'] == video_id and record['document']['media_fingerprint'] == 'original'
        recipe, _, _ = api._saved_generation(application_services.gemini_subtitle_jobs.get(job['id']), application_services)
        assert recipe['video_id'] == video_id


def test_regeneration_preserves_old_snapshot_and_binds_new_output(binding, monkeypatch, application_services):
    _, store = binding
    monkeypatch.setattr(application_services, 'gemini_subtitle_service', SimpleNamespace(
        cache_policy=lambda: {'version': 1}, generate=lambda *a: {'document': document('New subtitles')}))
    job = api.generate_subtitles_with_gemini_endpoint(GeminiSubtitleRequest(
        video_id='a' * 12, regenerate=True, current_document={**document(), 'media_fingerprint': 'old-video'}),
        services=application_services)
    result = _wait_for_state(application_services.gemini_subtitle_jobs, job['id'], {'succeeded'})['result']
    assert result['document']['media_fingerprint'] == 'original'
    versions = store.list('a' * 12)['versions']
    assert {row['media_fingerprint'] for row in versions} == {'original', 'old-video'}
