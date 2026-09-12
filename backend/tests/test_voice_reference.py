import array
import io
import wave
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.voiceover import build_voiceover_router
from app.middleware.auth import get_current_user
from app.services.voiceover.models import (
    MODEL_ID,
    MODEL_REVISION,
    SDK_VERSION,
    V2_MODEL_ID,
    V2_MODEL_REVISION,
    VoiceClip,
    VoiceDocument,
    VoiceProfile,
)
from app.services.voiceover.store import VoiceStore, digest, generation_hash, write_json


def test_reference_crop_uses_selected_start_and_rejects_invalid_ranges(tmp_path):
    source = io.BytesIO()
    with wave.open(source, 'wb') as wav:
        wav.setparams((1, 2, 48000, 0, 'NONE', 'none'))
        # Distinct constant signals let the test verify content, not just duration.
        wav.writeframes(array.array('h', [1000] * (48000 * 4) + [6000] * (48000 * 5)).tobytes())
    app = FastAPI()
    store = VoiceStore(tmp_path)
    app.include_router(build_voiceover_router(SimpleNamespace(store=store)))
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'user'}
    with TestClient(app) as client:
        files = {'file': ('sample.wav', source.getvalue(), 'audio/wav')}
        response = client.post('/api/v1/voiceover/references?start_seconds=4&duration_seconds=3', files=files)
        assert response.status_code == 200, response.text
        assert response.json()['duration_ms'] == 3000
        audio = client.get('/api/v1/voiceover/references/' + response.json()['id'])
        with wave.open(io.BytesIO(audio.content)) as wav:
            assert set(array.array('h', wav.readframes(wav.getnframes()))) == {6000}
        for query in ['start_seconds=-1', 'start_seconds=nan', 'duration_seconds=9',
                      'duration_seconds=2', 'duration_seconds=inf', 'start_seconds=8']:
            assert client.post('/api/v1/voiceover/references?' + query, files=files).status_code == 422


def test_old_voice_hash_preserved_and_processing_or_engine_invalidates_audio():
    profile = VoiceProfile(id='clone', name='Clone', reference_id='a' * 64)
    clip = VoiceClip(id='one', spoken_text='Xin chào', start_ms=0, end_ms=3000)
    doc = VoiceDocument(project_id='b' * 20, video_fingerprint='test', profile=profile, clips=[clip])
    old_profile = profile.model_dump(exclude={'denoise'})
    legacy_hash = digest({'pipeline': 1, 'sdk': SDK_VERSION, 'profile': old_profile,
                          'text': 'Xin chào', 'backend': 'cuda', 'precision': 'fp32', 'temperature': 0.8})
    assert generation_hash(doc, clip, 'cuda') == legacy_hash
    doc.profile.denoise = False
    raw_hash = generation_hash(doc, clip, 'cuda')
    assert raw_hash != legacy_hash
    doc.profile = VoiceProfile(**{**doc.profile.model_dump(), 'model_id': V2_MODEL_ID,
                                 'model_revision': V2_MODEL_REVISION})
    assert generation_hash(doc, clip, 'cuda') not in {legacy_hash, raw_hash}
    with pytest.raises(ValueError, match='Phiên bản'):
        VoiceProfile(id='wrong', name='Wrong', preset='voice', model_id=V2_MODEL_ID)


def test_engine_readiness_is_independent(tmp_path, monkeypatch):
    import os

    from app.services.voiceover import manager as module
    monkeypatch.setattr(module, 'RUNTIME', tmp_path)
    monkeypatch.delenv('CONTENT_BOT_VOICE_PYTHON', raising=False)
    python = tmp_path / '.venv-gpu' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    python.parent.mkdir(parents=True)
    python.touch()
    write_json(tmp_path / 'runtime-status-v2turbo.json', {
        'ready': True, 'devices': ['cuda'], 'sdk_version': SDK_VERSION,
        'model_revision': V2_MODEL_REVISION, 'presets': [],
    })
    manager = module.VoiceManager(VoiceStore(tmp_path / 'store'))
    try:
        assert not manager.status(MODEL_ID)['ready']
        assert manager.status(V2_MODEL_ID)['ready']
        assert manager.status(V2_MODEL_ID)['devices'] == ['cuda']
        write_json(tmp_path / 'runtime-status-v2turbo.json', {
            'ready': True, 'devices': ['cuda'], 'sdk_version': SDK_VERSION,
            'model_revision': MODEL_REVISION,
        })
        assert not manager.status(V2_MODEL_ID)['ready']
    finally:
        manager.shutdown()
