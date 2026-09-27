import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_subtitle_jobs import _wait_for_state

from app.api import subtitles as api
from app.main import app
from app.services.subtitle_jobs import SubtitleJobManager


@pytest.mark.parametrize('kind', ['ocr', 'asr', 'alignment', 'translation', 'generation', 'review', 'render', 'scene'])
def test_completed_job_revalidates_source_after_manager_restart(tmp_path, monkeypatch, application_services, kind):
    media = {'fingerprint': 'original'}
    monkeypatch.setattr(api, '_uploaded_video_path', lambda _: tmp_path / 'source.mp4')
    monkeypatch.setattr(api, 'probe_media_cached', lambda *a, **k: dict(media))
    manager = SubtitleJobManager(tmp_path / 'jobs')
    binding = {'video_id': 'a' * 12, 'media_fingerprint': 'original'}

    def run(context):
        context.update_details({'completed': 10})  # Progress cannot overwrite source identity.
        return {'fixture': 'completed result'}

    try:
        job = manager.submit(kind, 'b' * 64, run, source_binding=binding)
        binding['media_fingerprint'] = 'caller-mutated'
        _wait_for_state(manager, job['id'], {'succeeded'})
    finally:
        manager.shutdown()
    restored = SubtitleJobManager(tmp_path / 'jobs', max_cached=0)
    manager_name = 'gemini_subtitle_jobs' if kind in {'generation', 'review'} else 'subtitle_jobs'
    monkeypatch.setattr(application_services, manager_name, restored)
    prefix = 'gemini/' if kind in {'generation', 'review'} else ''
    endpoint = f"/api/v1/subtitles/{prefix}jobs/{job['id']}"
    original_json = (tmp_path / 'jobs' / f"{job['id']}.json").read_bytes()
    client = TestClient(app)
    try:
        valid = client.get(endpoint)
        assert valid.status_code == 200 and valid.json()['state'] == 'succeeded'
        media['fingerprint'] = 'replacement'
        invalid = client.get(endpoint)
        assert invalid.status_code == 200
        assert invalid.json()['state'] == 'failed' and invalid.json()['result'] is None
        assert invalid.json()['phase'] == 'source_changed'
        assert invalid.json()['details']['completed'] == 10
        assert invalid.json()['details']['source_validation_failed']
        assert restored.get(job['id'])['state'] == 'succeeded'
        assert (tmp_path / 'jobs' / f"{job['id']}.json").read_bytes() == original_json
        media['fingerprint'] = 'original'
        assert client.get(endpoint).json()['result'] == {'fixture': 'completed result'}
    finally:
        client.close()
        restored.shutdown()


def test_unavailable_source_is_not_returned_as_success(tmp_path, monkeypatch):
    def missing(_):
        raise HTTPException(404, 'Video missing')

    monkeypatch.setattr(api, '_uploaded_video_path', missing)
    job = {'state': 'succeeded', 'kind': 'ocr', 'result': {'document': {}},
           'source_binding': {'video_id': 'a' * 12, 'media_fingerprint': 'original'}}
    result = api._public_job(job)
    assert result['phase'] == 'source_unavailable' and result['result'] is None
    assert job['state'] == 'succeeded'


def test_legacy_generation_uses_durable_recipe_to_validate_source(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, 'content_bot_data_dir', tmp_path)
    monkeypatch.setattr(api, '_uploaded_video_path', lambda _: tmp_path / 'video.mp4')
    monkeypatch.setattr(api, 'probe_media_cached', lambda *a, **k: {'fingerprint': 'replacement'})
    recipes = tmp_path / 'gemini-runs'
    recipes.mkdir()
    (recipes / f"{'b' * 64}.json").write_text(json.dumps({
        'version': 1, 'video_id': 'a' * 12, 'media_fingerprint': 'original'}), encoding='utf-8')
    job = {'kind': 'generation', 'state': 'succeeded', 'dedupe_key': 'b' * 64, 'result': {'document': {}}}
    result = api._public_job(job)
    assert result['phase'] == 'source_changed' and result['result'] is None
