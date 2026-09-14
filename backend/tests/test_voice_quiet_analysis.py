import hashlib
import threading
import wave

import numpy as np
import pytest

from app.services.voiceover.quiet_analysis import analyze_quiet_edges


def write_wav(path, values):
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(np.asarray(values, dtype='<i2').tobytes())


def test_edges_are_measured_but_never_claimed_as_speech_or_auto_trim(tmp_path):
    wav = tmp_path / 'a.wav'
    write_wav(wav, [0] * 3200 + [4000, -4000] * 4000 + [0] * 4800)
    result = analyze_quiet_edges(wav, cache_dir=tmp_path / 'cache')
    assert result['duration_ms'] == 1000
    assert all(row['head_quiet_ms'] == 200 and row['tail_quiet_ms'] == 300 for row in result['thresholds'])
    assert result['protected_head_candidate_ms'] == 170
    assert result['protected_tail_candidate_ms'] == 270
    assert not result['speech_verified'] and not result['automatic_trim_allowed']
    assert analyze_quiet_edges(wav, cache_dir=tmp_path / 'cache')['cache_hit']


def test_faint_leading_sound_is_protected_and_cache_does_not_hide_changed_audio(tmp_path):
    wav = tmp_path / 'a.wav'
    write_wav(wav, [0] * 800 + [50, -50] * 1200 + [4000, -4000] * 4000)
    result = analyze_quiet_edges(wav, cache_dir=tmp_path / 'cache')
    assert result['thresholds'][0]['head_quiet_ms'] == 200
    assert result['protected_head_candidate_ms'] == 20
    old_checksum = hashlib.sha256(wav.read_bytes()).hexdigest()
    write_wav(wav, [4000, -4000] * 8000)
    with pytest.raises(ValueError, match='thay đổi'):
        analyze_quiet_edges(wav, expected_checksum=old_checksum, cache_dir=tmp_path / 'cache')
    assert not analyze_quiet_edges(wav, cache_dir=tmp_path / 'cache')['cache_hit']


def test_silent_audio_and_cancellation_do_not_produce_trim_instructions(tmp_path):
    wav = tmp_path / 'a.wav'
    write_wav(wav, [0] * 16000)
    result = analyze_quiet_edges(wav)
    assert not result['has_detectable_signal']
    assert result['protected_head_candidate_ms'] == result['protected_tail_candidate_ms'] == 0
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        analyze_quiet_edges(wav, cancel=cancel)


def test_quiet_analysis_endpoint_is_owner_scoped_and_reuses_cache(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_voiceover import asset, document

    from app.api.voiceover import build_voiceover_router
    from app.middleware.auth import get_current_user
    from app.services.voiceover.store import VoiceStore

    store = VoiceStore(tmp_path)
    meta = asset(store, document())
    app = FastAPI()
    app.include_router(build_voiceover_router(SimpleNamespace(store=store)))
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'user'}
    with TestClient(app) as client:
        url = f"/api/v1/voiceover/assets/{meta['id']}/quiet-analysis"
        first = client.get(url)
        assert first.status_code == 200
        assert not first.json()['cache_hit'] and not first.json()['automatic_trim_allowed']
        assert client.get(url).json()['cache_hit']
        app.dependency_overrides[get_current_user] = lambda: {'sub': 'other-user'}
        assert client.get(url).status_code == 404
