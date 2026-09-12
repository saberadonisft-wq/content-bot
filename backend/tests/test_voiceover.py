from __future__ import annotations

import array
import math
import threading
import wave
from pathlib import Path

import pytest


def test_worker_deadline_does_not_reset_for_messages_or_heartbeat():
    from app.services.voiceover.manager import WorkerDeadline
    deadline = WorkerDeadline(0)
    deadline.observe({'stage': 'loading', 'message': 'Still loading'}, 899)
    with pytest.raises(TimeoutError, match='Nạp model'):
        deadline.check(901)
    deadline.observe({'stage': 'generating', 'clip_id': 'one'}, 1000)
    deadline.check(2799)
    deadline.observe({'stage': 'generating', 'clip_id': 'one', 'heartbeat': 2800}, 2800)
    with pytest.raises(TimeoutError, match='đoạn one'):
        deadline.check(2801)
    deadline.observe({'stage': 'generating', 'clip_id': 'two'}, 2802)
    deadline.check(2803)
    deadline.observe({'stage': 'finished'}, 2804)
    deadline.check(10000)


def test_voice_cut_validation_uses_video_segment_rules():
    from app.services.subtitle_render import SubtitleRenderError
    from app.services.voiceover.mix import verify_voice_cuts
    doc = document()
    doc.clips[0].duration_ms = 1000
    # Adjacent retained pieces are continuous speech, not a cut through a word.
    verify_voice_cuts(doc, {'video_segments': [
        {'start_ms': 0, 'end_ms': 1500}, {'start_ms': 1500, 'end_ms': 3000},
    ]}, 3000)
    # Duplicate/overlapping pieces must not inflate overlap and conceal a cut.
    with pytest.raises(SubtitleRenderError, match='overlap'):
        verify_voice_cuts(doc, {'video_segments': [
            {'start_ms': 1000, 'end_ms': 1500}, {'start_ms': 1000, 'end_ms': 1500},
        ]}, 3000)
    with pytest.raises(ValueError, match='Điểm cắt'):
        verify_voice_cuts(doc, {'video_segments': [
            {'start_ms': 0, 'end_ms': 1400}, {'start_ms': 1600, 'end_ms': 3000},
        ]}, 3000)


def test_export_ignores_missing_voice_only_when_fully_removed(tmp_path):
    from app.services.voiceover.mix import export_voice_audio
    store = VoiceStore(tmp_path)
    doc = document()
    doc.clips.append(VoiceClip(id='missing', spoken_text='Phần đã bỏ', start_ms=4000, end_ms=6000))
    doc = store.save_document('user', doc)
    store.attach('user', doc.project_id, 'one', asset(store, doc))
    doc = store.get_document('user', doc.project_id)
    output = export_voice_audio(store, 'user', doc, 'wav', {'trim_end_ms': 3000}, 6000)
    with wave.open(str(output)) as audio:
        assert audio.getnframes() / audio.getframerate() == 3
    with pytest.raises(ValueError, match='chưa tạo'):
        export_voice_audio(store, 'user', doc, 'wav', {'trim_end_ms': 4500}, 6000)


def test_render_rejects_inconsistent_audio_duration(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document('user', document())
    meta = asset(store, doc)
    store.attach('user', doc.project_id, 'one', meta)
    doc = store.get_document('user', doc.project_id)
    doc.clips[0].duration_ms = 1
    with pytest.raises(ValueError, match='Thời lượng'):
        verify_document(store, 'user', doc, 3000)
    doc.clips[0].duration_ms = meta['duration_ms']
    meta['duration_ms'] = 1
    write_json(store.path('user', 'assets', meta['id']), meta)
    with pytest.raises(ValueError, match='Thời lượng'):
        verify_document(store, 'user', doc, 3000)


def test_composition_cancellation_precedes_asset_validation(tmp_path):
    from app.services.subtitle_render import SubtitleRenderCanceled
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SubtitleRenderCanceled):
        compose_voice(VoiceStore(tmp_path), 'user', document(), 3000, cancel)

from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.mix import compose_voice, verify_document
from app.services.voiceover.models import VoiceClip, VoiceDocument, VoiceProfile
from app.services.voiceover.store import VoiceStore, generation_hash, write_json


def test_audio_cancellation_uses_render_job_cancellation():
    from app.services.subtitle_render import SubtitleRenderCanceled
    from app.services.voiceover.mix import ffmpeg
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SubtitleRenderCanceled):
        ffmpeg(['-re', '-f', 'lavfi', '-i', 'anullsrc=r=48000', '-t', '30', '-f', 'null', '-'], cancel)


def test_duck_control_wave_matches_preview_timing(tmp_path):
    from app.services.voiceover.mix import write_duck_envelope
    doc = document()
    clip = doc.clips[0]
    clip.start_ms, clip.offset_ms, clip.duration_ms, clip.rate = 1000, 100, 1000, 2
    clip.asset_id = 'audio'
    path = tmp_path / 'duck.wav'
    write_duck_envelope(path, doc, 2000)
    with wave.open(str(path)) as audio:
        assert audio.getframerate() == 1000
        samples = array.array('h', audio.readframes(audio.getnframes()))
    for ms, expected in [(1099, 1), (1100, 1), (1110, .625), (1120, .25), (1600, .25), (1725, .625), (1850, 1)]:
        assert abs(samples[ms] / 32767 - expected) < .00004
    clip.gain = 0
    write_duck_envelope(path, doc, 2000)
    with wave.open(str(path)) as audio:
        assert min(array.array('h', audio.readframes(audio.getnframes()))) == 32767


def test_runtime_status_rejects_missing_python_and_handles_corrupt_status(tmp_path, monkeypatch):
    from app.services.voiceover import manager as module
    monkeypatch.setattr(module, 'RUNTIME', tmp_path)
    manager = module.VoiceManager(VoiceStore(tmp_path / 'store'))
    try:
        python = tmp_path / 'python.exe'
        monkeypatch.setattr(manager, 'python', lambda device='cpu': python)
        write_json(tmp_path / 'runtime-status.json', {'ready': True, 'devices': ['cpu', 'cuda']})
        assert manager.status()['ready'] is False
        assert manager.status()['devices'] == []
        python.touch()
        assert manager.status()['ready'] is False
        assert 'Phiên bản' in manager.status()['message']
        write_json(tmp_path / 'runtime-status.json', {'ready': True, 'devices': ['cpu'],
            'sdk_version': module.SDK_VERSION, 'model_revision': module.MODEL_REVISION})
        assert manager.status()['ready'] is True
        (tmp_path / 'runtime-status.json').write_text('{broken', encoding='utf-8')
        status = manager.status()
        assert status['installed'] is True
        assert status['ready'] is False
        assert 'setup-voiceover' in status['message']
    finally:
        manager.shutdown()


def test_gpu_worker_uses_isolated_python_and_respects_override(tmp_path, monkeypatch):
    import os

    from app.services.voiceover import manager as module
    monkeypatch.setattr(module, 'RUNTIME', tmp_path)
    monkeypatch.delenv('CONTENT_BOT_VOICE_PYTHON', raising=False)
    gpu = tmp_path / '.venv-gpu' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    gpu.parent.mkdir(parents=True)
    gpu.touch()
    manager = module.VoiceManager(VoiceStore(tmp_path / 'store'))
    try:
        assert manager.python('cuda') == gpu
        assert manager.python('cpu') != gpu
        write_json(tmp_path / 'runtime-status.json', {'ready': True, 'devices': ['cpu', 'cuda'],
            'sdk_version': module.SDK_VERSION, 'model_revision': module.MODEL_REVISION})
        assert manager.status()['installed'] is True
        assert manager.status()['ready'] is True
        assert manager.status()['devices'] == ['cuda']
        doc = manager.store.save_document('user', document())
        with pytest.raises(ValueError, match='cpu chưa sẵn sàng'):
            manager.start('user', doc.project_id, 'cpu')
        override = tmp_path / 'custom-python'
        monkeypatch.setenv('CONTENT_BOT_VOICE_PYTHON', str(override))
        assert manager.python('cuda') == override
    finally:
        manager.shutdown()


def test_final_voice_export_honors_cancel_before_composition(tmp_path):
    from app.services.subtitle_render import SubtitleRenderCanceled
    from app.services.voiceover.mix import export_voice_audio
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SubtitleRenderCanceled):
        export_voice_audio(VoiceStore(tmp_path), 'user', document(), 'wav', cancel=cancel)


@pytest.mark.parametrize('clip_rate,video_speed', [(0.5, 0.5), (2, 2)])
def test_export_combines_clip_and_video_speed_without_filter_limits(tmp_path, clip_rate, video_speed):
    from app.services.voiceover.mix import export_voice_audio
    store = VoiceStore(tmp_path)
    doc = document()
    doc.clips[0].rate = clip_rate
    doc = store.save_document('user', doc)
    store.attach('user', doc.project_id, 'one', asset(store, doc))
    doc = store.get_document('user', doc.project_id)
    output = export_voice_audio(store, 'user', doc, 'wav', {'video_speed': video_speed}, 4000)
    with wave.open(str(output), 'rb') as wav:
        assert abs(wav.getnframes() / wav.getframerate() - 4 / video_speed) <= 0.020


def test_final_mp4_voice_onset_after_slow_video_edit(tmp_path):
    import subprocess

    import imageio_ffmpeg

    from app.services.media_probe import probe_media
    from app.services.voiceover.mix import ffmpeg, finalize_voiced_render
    source = tmp_path / 'edited.mp4'
    ffmpeg(['-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=25:d=6',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)])
    store = VoiceStore(tmp_path / 'store')
    doc = store.save_document('user', document())
    store.attach('user', doc.project_id, 'one', asset(store, doc))
    doc = store.get_document('user', doc.project_id)
    result = finalize_voiced_render(store, 'user', doc,
        {'output_filename': source.name, 'video_id': 'a' * 20, 'duration_ms': 6000},
        tmp_path, {'duration_ms': 4000, 'has_audio': False},
        {'trim_start_ms': 500, 'trim_end_ms': 3500, 'video_speed': 0.5}, 1)
    final = tmp_path / result['output_filename']
    assert abs(probe_media(final)['duration_ms'] - 6000) <= 40
    decoded = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-i', str(final),
        '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 's16le', '-'],
        capture_output=True, check=True, timeout=30)
    samples = array.array('h', decoded.stdout)
    onset = next(i for i, value in enumerate(samples) if abs(value) > 400) / 48000
    assert abs(onset - 1.0) <= 0.020


@pytest.mark.parametrize('speed', [0.5, 1, 1.5, 2])
def test_multiple_audio_cuts_preserve_tone_onsets(tmp_path, speed):
    from app.services.voiceover.mix import export_voice_audio
    store = VoiceStore(tmp_path)
    doc = document()
    doc.clips = [VoiceClip(id=f'clip{i}', spoken_text='Xin chào', start_ms=start,
                           end_ms=start + 1000, rate=1) for i, start in enumerate([1000, 5000, 9000])]
    doc = store.save_document('user', doc)
    meta = asset(store, doc)
    for clip in doc.clips:
        store.attach('user', doc.project_id, clip.id, meta)
    doc = store.get_document('user', doc.project_id)
    segments = [{'start_ms': start, 'end_ms': start + 2000} for start in [500, 4500, 8500]]
    path = export_voice_audio(store, 'user', doc, 'wav', {'video_segments': segments, 'video_speed': speed}, 11000)
    with wave.open(str(path), 'rb') as audio:
        samples = array.array('h', audio.readframes(audio.getnframes()))
        sr = audio.getframerate()
    for source_onset in [0.5, 2.5, 4.5]:
        expected = source_onset / speed
        lower, upper = max(0, round((expected - 0.1) * sr)), round((expected + 0.1) * sr)
        actual = next(i for i in range(lower, upper) if abs(samples[i]) > 400) / sr
        assert abs(actual - expected) <= 0.020, (speed, expected, actual)


def document():
    return VoiceDocument(
        project_id="a" * 20,
        video_fingerprint="video",
        profile=VoiceProfile(id="default", name="Ngọc Huyền", preset="Ngọc Huyền"),
        clips=[
            VoiceClip(
                id="one", spoken_text="Xin chào", start_ms=1000, end_ms=3000, rate=1
            )
        ],
    )


def test_failed_segment_does_not_override_new_text_and_recovers_on_success(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document('user', document())
    original_hash = generation_hash(doc, doc.clips[0], 'cpu')
    assert store.mark_failed('user', doc.project_id, 'one', original_hash, 'cpu', 'model failed')
    failed = store.get_document('user', doc.project_id)
    assert failed.clips[0].status == 'failed'
    assert not store.mark_failed('user', doc.project_id, 'one', original_hash, 'cpu', 'model failed')
    failed.clips[0].spoken_text = 'New text'
    edited = store.save_document('user', failed)
    assert not store.mark_failed('user', doc.project_id, 'one', original_hash, 'cpu', 'old error')
    store.attach('user', doc.project_id, 'one', asset(store, edited))
    recovered = store.get_document('user', doc.project_id)
    assert recovered.clips[0].status == 'ready'
    assert recovered.clips[0].error is None


def test_voice_audio_export_applies_trim_and_video_speed(tmp_path):
    from app.services.voiceover.mix import export_voice_audio
    store = VoiceStore(tmp_path)
    doc = store.save_document('user', document())
    store.attach('user', doc.project_id, 'one', asset(store, doc))
    doc = store.get_document('user', doc.project_id)
    path = export_voice_audio(store, 'user', doc, 'wav',
                              {'trim_start_ms': 1000, 'trim_end_ms': 3000, 'video_speed': 2}, 4000)
    with wave.open(str(path), 'rb') as audio:
        assert abs(audio.getnframes() / audio.getframerate() - 1) < 0.04
    with pytest.raises(ValueError, match='Điểm cắt'):
        export_voice_audio(store, 'user', doc, 'wav', {'trim_start_ms': 1250}, 4000)


def test_audio_export_endpoint_revision_cuts_and_owner(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.voiceover import build_voiceover_router
    from app.middleware.auth import get_current_user
    store = VoiceStore(tmp_path)
    doc = store.save_document('user', document())
    store.attach('user', doc.project_id, 'one', asset(store, doc))
    doc = store.get_document('user', doc.project_id)
    app = FastAPI()
    app.include_router(build_voiceover_router(SimpleNamespace(store=store)))
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'user'}
    payload = {'revision': doc.revision, 'duration_ms': 4000, 'format': 'wav', 'video_speed': 2,
               'trim_start_ms': 1000, 'trim_end_ms': 3000}
    with TestClient(app) as client:
        endpoint = f'/api/v1/voiceover/projects/{doc.project_id}/audio'
        response = client.post(endpoint, json=payload)
        assert response.status_code == 200
        assert response.content[:4] == b'RIFF'
        assert client.post(endpoint, json={**payload, 'revision': 0}).status_code == 409
        assert client.post(endpoint, json={**payload, 'trim_start_ms': 1250}).status_code == 409
        assert client.post(endpoint, json={**payload, 'video_speed': 0}).status_code == 422
        app.dependency_overrides[get_current_user] = lambda: {'sub': 'other-user'}
        assert client.post(endpoint, json=payload).status_code == 404


def test_waveform_api_returns_bounded_level_and_checks_owner(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.voiceover import build_voiceover_router
    from app.middleware.auth import get_current_user
    store = VoiceStore(tmp_path)
    key = 'a' * 64
    write_json(store.path('user', 'assets', key), {
        'duration_ms': 100000, 'peaks': [[0.1] * 5000, [0.2] * 1250, [0.3] * 313, [0.4] * 79, [0.5] * 20],
    })
    app = FastAPI()
    app.include_router(build_voiceover_router(SimpleNamespace(store=store)))
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'user'}
    with TestClient(app) as client:
        endpoint = f'/api/v1/voiceover/assets/{key}/peaks'
        response = client.get(endpoint)
        assert response.status_code == 200
        assert len(response.json()['peaks'][0]) == 79
        assert len(client.get(endpoint + '?max_points=1500').json()['peaks'][0]) == 1250
        assert len(client.get(endpoint + '?max_points=16').json()['peaks'][0]) <= 16
        assert client.get(endpoint + '?max_points=99999').status_code == 422
        app.dependency_overrides[get_current_user] = lambda: {'sub': 'other'}
        assert client.get(endpoint).status_code == 404


def test_invalid_reference_audio_returns_actionable_error(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.voiceover import build_voiceover_router
    from app.middleware.auth import get_current_user
    app = FastAPI()
    app.include_router(build_voiceover_router(SimpleNamespace(store=VoiceStore(tmp_path))))
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'user'}
    with TestClient(app) as client:
        response = client.post('/api/v1/voiceover/references', files={'file': ('bad.wav', b'not audio', 'audio/wav')})
    assert response.status_code == 422
    assert 'mẫu giọng' in response.json()['detail']


def test_package_deduplicates_audio_shared_by_repeated_lines(tmp_path):
    import zipfile

    from app.services.voiceover.manager import VoiceManager
    from app.services.voiceover.packages import export_package
    store = VoiceStore(tmp_path)
    manager = VoiceManager(store)
    try:
        doc = document()
        doc.clips.append(doc.clips[0].model_copy(update={'id': 'two', 'start_ms': 4000, 'end_ms': 6000}))
        doc = store.save_document('user', doc)
        meta = asset(store, doc)
        package = export_package(manager, 'user', doc.project_id, 'cpu')
        with zipfile.ZipFile(package) as bundle:
            names = bundle.namelist()
            assert len(names) == len(set(names))
            assert names.count(f'assets/{meta["id"]}.wav') == 1
    finally:
        manager.shutdown()


def asset(store, doc):
    key = generation_hash(doc, doc.clips[0], "cpu")
    path = store.path("user", "assets", key, ".wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "none"))
        values = array.array(
            "h",
            [
                round(3000 * math.sin(i * 2 * math.pi * 440 / 48000))
                for i in range(24000)
            ],
        )
        wav.writeframes(values.tobytes())
    meta = audio_metadata(path)
    meta.update(id=key, generation_hash=key, device="cpu")
    write_json(store.path("user", "assets", key), meta)
    return meta


def test_late_generation_does_not_overwrite_edited_text(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document("user", document())
    meta = asset(store, doc)
    doc.clips[0].spoken_text = "Câu đã sửa"
    store.save_document("user", doc)
    assert not store.attach("user", doc.project_id, "one", meta)
    assert store.get_document("user", doc.project_id).clips[0].asset_id is None


@pytest.mark.parametrize('failure', ['timeout', 'crash', 'cancel'])
def test_supervisor_stops_real_worker_and_preserves_committed_audio(tmp_path, monkeypatch, failure):
    import sys
    import time

    from app.services.voiceover import manager as module
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    worker = runtime / 'worker.py'
    worker.write_text("import sys,time\n" + ("sys.exit(7)\n" if failure == 'crash' else "time.sleep(120)\n"), encoding='utf-8')
    monkeypatch.setattr(module, 'RUNTIME', runtime)
    store = VoiceStore(tmp_path / 'store')
    doc = document()
    doc.clips.append(VoiceClip(id='two', spoken_text='Đoạn chưa tạo', start_ms=4000, end_ms=6000))
    doc = store.save_document('user', doc)
    meta = asset(store, doc)
    store.attach('user', doc.project_id, 'one', meta)
    committed = store.path('user', 'assets', meta['id'], '.wav').read_bytes()
    manager = module.VoiceManager(store)
    monkeypatch.setattr(manager, 'python', lambda device='cpu': Path(sys.executable))
    monkeypatch.setattr(manager, 'status', lambda: {'ready': True, 'devices': ['cpu']})
    processes = []
    original_popen = module.subprocess.Popen
    deadline_class = module.WorkerDeadline
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    if failure == 'timeout':
        class ShortDeadline(module.WorkerDeadline):
            def check(self, now):
                # Exercise the real production timeout branch without a 15-minute wait.
                super().check(now + 900)
        monkeypatch.setattr(module, 'WorkerDeadline', ShortDeadline)
    try:
        job = manager.start('user', doc.project_id, 'cpu')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = manager.get('user', job['id'])
            if failure == 'cancel' and processes:
                manager.control('user', job['id'], 'cancel')
            if job['state'] not in {'queued', 'running'}:
                break
            time.sleep(.05)
        assert job['state'] == ('canceled' if failure == 'cancel' else 'failed'), job
        assert processes and all(process.poll() is not None for process in processes)
        assert job['completed'] == 1
        restored = store.get_document('user', doc.project_id)
        assert restored.clips[0].asset_id == meta['id']
        assert restored.clips[1].asset_id is None
        assert store.path('user', 'assets', meta['id'], '.wav').read_bytes() == committed
        monkeypatch.setattr(module, 'WorkerDeadline', deadline_class)
        source_wav = str(store.path('user', 'assets', meta['id'], '.wav'))
        worker.write_text(
            'import sys,json,hashlib,shutil\nfrom pathlib import Path\n'
            'root=Path(sys.argv[2])\n'
            'manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))\n'
            'assert [item["id"] for item in manifest["clips"]] == ["two"]\n'
            'item=manifest["clips"][0]; key=item["generation_hash"]\n'
            'wav=root/"assets"/(key+".wav")\n'
            f'shutil.copyfile({source_wav!r},wav)\n'
            '(wav.with_suffix(".json")).write_text(json.dumps({"checksum":hashlib.sha256(wav.read_bytes()).hexdigest(),"generation_hash":key}))\n'
            '(root/"progress.json").write_text(json.dumps({"stage":"finished","completed":["two"],"failed":[],"message":"done"}))\n',
            encoding='utf-8')
        resumed = manager.control('user', job['id'], 'resume')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            resumed = manager.get('user', resumed['id'])
            if resumed['state'] not in {'queued', 'running'}:
                break
            time.sleep(.05)
        assert resumed['id'] != job['id']
        assert resumed['state'] == 'succeeded', resumed
        assert resumed['completed'] == 2
        assert store.path('user', 'assets', meta['id'], '.wav').read_bytes() == committed
    finally:
        manager.shutdown()


def test_timing_change_reuses_audio_but_text_change_invalidates(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document("user", document())
    meta = asset(store, doc)
    assert store.attach("user", doc.project_id, "one", meta)
    doc = store.get_document("user", doc.project_id)
    doc.clips[0].offset_ms = 500
    doc = store.save_document("user", doc)
    assert doc.clips[0].status == "ready"
    doc.clips[0].spoken_text = "Một câu khác"
    doc = store.save_document("user", doc)
    assert doc.clips[0].status == "stale"
    with pytest.raises(ValueError, match="thay đổi"):
        verify_document(store, "user", doc, 5000)


def test_compose_places_audio_and_preserves_silence(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document("user", document())
    store.attach("user", doc.project_id, "one", asset(store, doc))
    doc = store.get_document("user", doc.project_id)
    output = compose_voice(store, "user", doc, 4000)
    with wave.open(str(output), "rb") as wav:
        assert wav.getnframes() == 192000
        assert not any(wav.readframes(47000))
        wav.setpos(49000)
        assert any(wav.readframes(1000))


def test_composition_reuses_audio_across_document_revisions(tmp_path, monkeypatch):
    from app.services.voiceover import mix
    store = VoiceStore(tmp_path)
    doc = store.save_document('user', document())
    store.attach('user', doc.project_id, 'one', asset(store, doc))
    doc = store.get_document('user', doc.project_id)
    first = compose_voice(store, 'user', doc, 4000)
    doc.revision += 1
    doc.mix.original_gain = .6
    doc.clips[0].end_ms += 100
    original_ffmpeg = mix.ffmpeg
    def unexpected(*args, **kwargs):
        raise AssertionError('Unchanged PCM track should not invoke ffmpeg')
    monkeypatch.setattr(mix, 'ffmpeg', unexpected)
    assert compose_voice(store, 'user', doc, 4000) == first
    monkeypatch.setattr(mix, 'ffmpeg', original_ffmpeg)
    doc.clips[0].offset_ms += 100
    assert compose_voice(store, 'user', doc, 4000) != first


def test_owner_isolation_and_revision_conflict(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document("user", document())
    with pytest.raises(FileNotFoundError):
        store.get_document("another", doc.project_id)
    stale = doc.model_copy(deep=True)
    store.save_document("user", doc)
    with pytest.raises(ValueError, match="thay đổi"):
        store.save_document("user", stale)


def test_portable_package_roundtrip_and_newer_local_edit(tmp_path):
    import json
    import zipfile

    from app.services.voiceover.manager import VoiceManager
    from app.services.voiceover.packages import export_package, import_package

    store = VoiceStore(tmp_path)
    manager = VoiceManager(store)
    try:
        doc = store.save_document("user", document())
        meta = asset(store, doc)
        package = export_package(manager, "user", doc.project_id, "cpu")
        with zipfile.ZipFile(package) as z:
            manifest = z.read("manifest.json")
            assert "worker.py" in z.namelist()
        result = tmp_path / "results.zip"
        with zipfile.ZipFile(result, "w") as z:
            z.writestr("manifest.json", manifest)
            z.writestr('environment.txt', 'vieneu==3.6.4\n')
            z.write(
                store.path("user", "assets", meta["id"], ".wav"),
                f"assets/{meta['id']}.wav",
            )
            z.writestr(f"assets/{meta['id']}.json", json.dumps(meta))
        imported = import_package(manager, "user", doc.project_id, result)
        assert imported.clips[0].asset_id == meta["id"]
        imported.clips[0].spoken_text = "Nội dung đã sửa tại máy"
        store.save_document("user", imported)
        again = import_package(manager, "user", doc.project_id, result)
        assert again.clips[0].spoken_text == "Nội dung đã sửa tại máy"
        assert again.clips[0].status == "stale"
    finally:
        manager.shutdown()


def test_package_rejects_zip_escape(tmp_path):
    import zipfile

    from app.services.voiceover.manager import VoiceManager
    from app.services.voiceover.packages import import_package

    store = VoiceStore(tmp_path)
    manager = VoiceManager(store)
    try:
        path = tmp_path / "bad.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("../outside.txt", "bad")
        with pytest.raises(ValueError, match="đường dẫn"):
            import_package(manager, "user", "a" * 20, path)
        assert not (tmp_path.parent / "outside.txt").exists()
    finally:
        manager.shutdown()


def test_cut_can_remove_whole_line_but_cannot_truncate_speech():
    from app.services.voiceover.mix import verify_voice_cuts

    doc = document()
    doc.clips[0].duration_ms = 1000
    verify_voice_cuts(
        doc, {"video_segments": [{"start_ms": 3000, "end_ms": 5000}]}, 5000
    )
    verify_voice_cuts(
        doc, {"video_segments": [{"start_ms": 500, "end_ms": 2500}]}, 5000
    )
    with pytest.raises(ValueError, match="Điểm cắt"):
        verify_voice_cuts(doc, {"trim_start_ms": 1500}, 5000)


def test_corrupted_asset_rejected_before_render(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document("user", document())
    meta = asset(store, doc)
    store.attach("user", doc.project_id, "one", meta)
    doc = store.get_document("user", doc.project_id)
    path = store.path("user", "assets", meta["id"], ".wav")
    data = bytearray(path.read_bytes())
    data[-100] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match="bị thay đổi"):
        verify_document(store, "user", doc, 4000)


@pytest.mark.parametrize("audio_format", ["wav", "flac", "mp3"])
def test_audio_export_real_codec(tmp_path, audio_format):
    import subprocess

    import imageio_ffmpeg

    from app.services.voiceover.mix import export_voice_audio

    store = VoiceStore(tmp_path)
    doc = store.save_document("user", document())
    store.attach("user", doc.project_id, "one", asset(store, doc))
    doc = store.get_document("user", doc.project_id)
    path = export_voice_audio(store, "user", doc, audio_format)
    decoded = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert 2900 <= len(decoded.stdout) / 96 <= 3100
    expected = {"wav": "pcm_s16le", "flac": "flac", "mp3": "mp3"}[audio_format]
    assert f"Audio: {expected}" in decoded.stderr.decode(errors="replace")


@pytest.mark.parametrize(
    "has_audio,mode,channels", [(False, "voice", 1), (True, "mix", 1), (True, "duck", 1), (True, "duck", 2)]
)
def test_real_video_mix_keeps_duration_and_adds_voice(tmp_path, has_audio, mode, channels):
    import subprocess

    import imageio_ffmpeg

    from app.services.media_probe import probe_media
    from app.services.voiceover.mix import ffmpeg, prepare_voiced_video

    source = tmp_path / "source.mp4"
    args = ["-f", "lavfi", "-i", "color=c=black:s=160x90:r=25:d=4"]
    if has_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=4"]
    ffmpeg([*args, "-ac", str(channels), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "4", str(source)])
    media = probe_media(source)
    store = VoiceStore(tmp_path / "store")
    doc = store.save_document("user", document())
    store.attach("user", doc.project_id, "one", asset(store, doc))
    doc = store.get_document("user", doc.project_id)
    doc.mix.mode = mode
    output = prepare_voiced_video(store, "user", doc, source, media, 1)
    result = probe_media(output)
    assert result["has_audio"]
    assert result['audio_channels'] == channels
    assert abs(result["duration_ms"] - 4000) <= 40
    decoded = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-v",
            "error",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert any(decoded.stdout[48000 * 2 : 72000 * 2])
    if has_audio:
        samples = array.array('h', decoded.stdout)
        def original_amplitude(start_seconds):
            start = round(start_seconds * 48000)
            values = samples[start:start + 4800]
            # Separate the original 220 Hz tone from the 440 Hz narration.
            real = sum(value * math.cos(2 * math.pi * 220 * i / 48000) for i, value in enumerate(values))
            imag = sum(value * math.sin(2 * math.pi * 220 * i / 48000) for i, value in enumerate(values))
            return math.hypot(real, imag) * 2 / len(values)
        before = original_amplitude(.5)
        during = original_amplitude(1.2)
        after = original_amplitude(2)
        assert abs(during / before - (.25 if mode == 'duck' else 1)) < .02
        assert abs(after / before - 1) < .02
