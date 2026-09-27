import errno
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_subtitle_jobs import _wait_for_state

from app.api import subtitles as api
from app.schemas import SubtitleRenderRequestV2
from app.services.subtitle_jobs import SubtitleJobManager
from app.services.subtitle_output_bindings import publish_render


@pytest.fixture
def render_fixture(tmp_path, monkeypatch):
    video_id = 'a' * 12
    filename = f"subtitled_{video_id}_{'b' * 12}.mp4"
    media = {'fingerprint': 'source-original', 'duration_ms': 2000, 'has_audio': False}
    monkeypatch.setattr(api.settings, 'content_bot_data_dir', tmp_path)
    monkeypatch.setattr(api, '_uploaded_video_path', lambda _: tmp_path / 'source.mp4')
    monkeypatch.setattr(api, 'probe_media_cached', lambda *a, **k: dict(media))
    output = tmp_path / 'videos' / 'output' / filename
    output.parent.mkdir(parents=True)
    return video_id, filename, media, output


def test_download_requires_publication_and_current_source(render_fixture):
    video_id, filename, media, output = render_fixture
    output.write_bytes(b'fixture output')
    with pytest.raises(HTTPException) as error:
        api.get_precision_subtitle_render(filename)
    assert error.value.status_code == 409
    publish_render(output.parent, filename, video_id, media['fingerprint'])
    assert api.get_precision_subtitle_render(filename).path == output
    media['fingerprint'] = 'replacement'
    with pytest.raises(HTTPException) as error:
        api.get_precision_subtitle_render(filename)
    assert error.value.status_code == 409
    assert output.read_bytes() == b'fixture output'
    media['fingerprint'] = 'source-original'
    output.write_bytes(b'replaced output with different bytes')
    with pytest.raises(HTTPException) as error:
        api.get_precision_subtitle_render(filename)
    assert error.value.status_code == 409


def test_legacy_subtitled_download_requires_current_publication(render_fixture):
    video_id, _, media, output = render_fixture
    filename = f"subtitled_{video_id}.mp4"
    output = output.parent / filename
    output.write_bytes(b'legacy output')
    publish_render(output.parent, filename, video_id, media['fingerprint'])
    assert api.get_subtitle_video(video_id, type='subtitled').path == output
    media['fingerprint'] = 'replacement'
    with pytest.raises(HTTPException) as error:
        api.get_subtitle_video(video_id, type='subtitled')
    assert error.value.status_code == 409


@pytest.mark.parametrize('failure', ['changed_source', 'publication_write', 'none'])
def test_render_only_publishes_after_source_check(tmp_path, render_fixture, monkeypatch, failure):
    video_id, filename, media, output = render_fixture
    manager = SubtitleJobManager(tmp_path / 'jobs')

    def render(*args, **kwargs):
        output.write_bytes(b'fixture encoded video')
        if failure == 'changed_source':
            media['fingerprint'] = 'replacement'
        return {'video_id': video_id, 'output_filename': filename, 'duration_ms': 2000}

    monkeypatch.setattr(api, 'render_precision_video', render)
    if failure == 'publication_write':
        def fail(*args):
            raise OSError(errno.ENOSPC, 'fixture publication disk full')
        monkeypatch.setattr(api, 'publish_render', fail)
    try:
        job = api.render_subtitle_timeline_v2_endpoint(SubtitleRenderRequestV2(
            video_id=video_id, document={'segments': [{'id': 'one', 'start_ms': 0, 'end_ms': 1000, 'text': 'Fixture'}]}), user={'sub': 'fixture'},
            services=SimpleNamespace(subtitle_jobs=manager))
        complete = _wait_for_state(manager, job['id'], {'succeeded', 'failed'})
        assert complete['state'] == ('succeeded' if failure == 'none' else 'failed')
        if failure == 'none':
            assert api.get_precision_subtitle_render(filename).path == output
            publication = output.parent / '.publications' / f'{filename}.json'
            publication.unlink()
            assert api.get_subtitle_job(job['id'], services=SimpleNamespace(subtitle_jobs=manager))['phase'] == 'output_unavailable'
            retried = api.render_subtitle_timeline_v2_endpoint(SubtitleRenderRequestV2(
                video_id=video_id, document={'segments': [{'id': 'one', 'start_ms': 0, 'end_ms': 1000, 'text': 'Fixture'}]}),
                user={'sub': 'fixture'}, services=SimpleNamespace(subtitle_jobs=manager))
            assert retried['id'] != job['id']
            _wait_for_state(manager, retried['id'], {'succeeded'})
            assert api.get_precision_subtitle_render(filename).path == output
        else:
            assert complete['result'] is None
            with pytest.raises(HTTPException) as error:
                api.get_precision_subtitle_render(filename)
            assert error.value.status_code == 409
    finally:
        manager.shutdown()
