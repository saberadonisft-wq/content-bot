"""Resource/lifecycle checks use tiny subprocesses and WAVs, never a TTS model."""
import array
import importlib.util
import json
import os
import subprocess
import sys
import time
import wave
from types import SimpleNamespace

import pytest

from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.manager import RUNTIME, VoiceManager, stop_worker
from app.services.voiceover.models import VoiceClip, VoiceDocument
from app.services.voiceover.store import VoiceStore, generation_hash, write_json


def worker_module():
    spec = importlib.util.spec_from_file_location("voice_worker_test", RUNTIME / "worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def spawn(script, **kwargs):
    return subprocess.Popen([sys.executable, "-c", script],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def wait_file(path):
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert path.exists()


WORKER_IMPORT = f"import runpy; w=runpy.run_path({str(RUNTIME / 'worker.py')!r}); "


def test_worker_lock_serializes_processes_and_releases_after_crash(tmp_path):
    env = dict(os.environ, VOICE_WORKER_LOCK=str(tmp_path / 'slot.lock'))
    ready = tmp_path / 'ready'
    second_ready = tmp_path / 'second'
    first = spawn(WORKER_IMPORT + f"\nwith w['worker_slot']({str(tmp_path)!r}) as acquired:\n"
                  f" from pathlib import Path\n Path({str(ready)!r}).touch()\n import time; time.sleep(60)", env=env)
    second = None
    try:
        wait_file(ready)
        second = spawn(WORKER_IMPORT + f"\nwith w['worker_slot']({str(tmp_path)!r}) as acquired:\n"
                       f" from pathlib import Path\n Path({str(second_ready)!r}).touch()", env=env)
        time.sleep(.4)
        assert not second_ready.exists()
        stop_worker(first)
        first.wait(timeout=5)
        assert second.wait(timeout=8) == 0
        assert second_ready.exists()
    finally:
        for process in (first, second):
            if process and process.poll() is None:
                stop_worker(process)
                process.wait(timeout=5)


def test_supervisor_loss_stops_busy_worker(tmp_path):
    heartbeat = tmp_path / 'heartbeat'
    heartbeat.touch()
    process = spawn(WORKER_IMPORT +
                    f"w['watch_supervisor']({str(heartbeat)!r}, timeout=.6, interval=.05); "
                    "import time; time.sleep(60)")
    try:
        for _ in range(5):
            heartbeat.touch()
            time.sleep(.2)
        assert process.poll() is None
        assert process.wait(timeout=5) == 75
    finally:
        if process.poll() is None:
            stop_worker(process)
            process.wait(timeout=5)


def test_pause_while_waiting_does_not_load_model(tmp_path, monkeypatch):
    module = worker_module()
    monkeypatch.setenv('VOICE_WORKER_LOCK', str(tmp_path / 'slot.lock'))
    waiting = tmp_path / 'waiting'
    waiting.mkdir()
    with module.worker_slot(tmp_path):
        process = spawn(WORKER_IMPORT + f"w['run']({str(waiting)!r})")
        try:
            wait_file(waiting / 'progress.json')
            assert process.poll() is None
            write_json(waiting / 'control.json', {'action': 'pause'})
            # There is no manifest or model in this directory: loading would fail.
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                stop_worker(process)
                process.wait(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='Windows venv process-tree regression')
def test_cancel_stops_child_as_well_as_launcher(tmp_path):
    pulse = tmp_path / 'pulse'
    child = f"import time\nfrom pathlib import Path\nwhile True:\n Path({str(pulse)!r}).touch()\n time.sleep(.05)"
    process = spawn(f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)")
    try:
        wait_file(pulse)
        stop_worker(process)
        process.wait(timeout=5)
        time.sleep(.2)
        stamp = pulse.stat().st_mtime_ns
        time.sleep(.3)
        assert pulse.stat().st_mtime_ns == stamp
    finally:
        if process.poll() is None:
            stop_worker(process)
            process.wait(timeout=5)


@pytest.mark.parametrize('device', ['cpu', 'cuda'])
def test_compute_caps_native_pools_without_loading_model(monkeypatch, device):
    calls = []
    class Options:
        intra_op_num_threads = 0
        inter_op_num_threads = 0
        def add_session_config_entry(self, key, value):
            calls.append((key, value))
    class Session:
        def __init__(self, path, options, **kwargs):
            self.options = options
    ort = SimpleNamespace(InferenceSession=Session, SessionOptions=Options)
    monkeypatch.setitem(sys.modules, 'onnxruntime', ort)
    monkeypatch.setitem(sys.modules, 'torch', None if device == 'cpu' else SimpleNamespace(
        set_num_threads=lambda n: calls.append(('torch', n)),
        set_num_interop_threads=lambda n: calls.append(('interop', n))))
    for key in ('CONTENT_BOT_VOICE_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'OMP_WAIT_POLICY'):
        monkeypatch.setenv(key, '3')
    worker_module().configure_compute(device)
    options = Options()
    options.intra_op_num_threads = 16
    session = ort.InferenceSession('unused', sess_options=options)
    assert session.options.intra_op_num_threads == 3
    assert session.options.inter_op_num_threads == 1
    assert (('torch', 3) in calls) == (device == 'cuda')
    assert (('interop', 1) in calls) == (device == 'cuda')
    assert ('session.intra_op.allow_spinning', '0') in calls
    assert os.environ['OMP_WAIT_POLICY'] == 'PASSIVE'


def test_batch_preserves_input_order_and_isolates_failed_rows(monkeypatch):
    module = worker_module()
    batches = []
    class Engine:
        def infer_batch(self, texts, **kwargs):
            batches.append(texts)
            if 'bad' in texts:
                raise RuntimeError('batch failed')
            return [text + '-audio' for text in texts]
        def infer(self, text, **kwargs):
            if text == 'bad':
                raise RuntimeError('bad row')
            return text + '-audio'
    items = [{'text': text} for text in ['first', 'bad', 'third', 'last']]
    # Fake GPU cache operations; no torch/GPU in this test.
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: None)))
    engine = Engine()
    results = module.infer_group(engine, items, {}, 'cuda')
    assert results == [('first-audio', None), (None, 'bad row'), ('third-audio', None), ('last-audio', None)]
    assert engine._content_bot_batch_limit == 1
    count = len(batches)
    assert module.infer_group(engine, [{'text': 'third'}, {'text': 'last'}], {}, 'cuda') == [
        ('third-audio', None), ('last-audio', None)]
    assert len(batches) == count


def test_batch_settings_keep_cpu_and_v2_single(monkeypatch):
    module = worker_module()
    monkeypatch.setenv('CONTENT_BOT_VOICE_BATCH_SIZE', '99')
    assert module.batch_limit('cpu') == 1
    assert module.batch_limit('cuda', module.V2_MODEL) == 1
    assert module.batch_limit('cuda') == 4
    monkeypatch.setenv('CONTENT_BOT_VOICE_BATCH_SIZE', 'invalid')
    assert module.batch_limit('cuda') == 2


def test_worker_batch_pause_preserves_each_cue_and_resumes_only_missing(tmp_path, monkeypatch):
    import numpy as np
    module = worker_module()
    monkeypatch.setenv('CONTENT_BOT_VOICE_BATCH_SIZE', '2')
    items = [{'id': str(i), 'text': f'cue {i}', 'generation_hash': f'{i:064x}'} for i in range(3)]
    write_json(tmp_path / 'manifest.json', {
        'schema_version': 1, 'sdk_version': module.SDK, 'device': 'cuda', 'clips': items,
        'profile': {'model_id': module.MODEL, 'model_revision': module.REVISION, 'preset': 'test'},
    })
    calls = []
    class Engine:
        sample_rate = 24000
        def infer_batch(self, texts, **kwargs):
            calls.append(texts)
            write_json(tmp_path / 'control.json', {'action': 'pause'})
            return [np.full(2400 + i * 100, .2, dtype=np.float32) for i in range(len(texts))]
        def infer(self, text, **kwargs):
            calls.append([text])
            return np.full(2500, .3, dtype=np.float32)
    def write_wav(path, audio, sr, **kwargs):
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, sr, 0, 'NONE', 'none'))
            wav.writeframes((audio * 32767).astype(np.int16).tobytes())
    def info(path):
        with wave.open(str(path), 'rb') as wav:
            return SimpleNamespace(frames=wav.getnframes(), samplerate=wav.getframerate(), channels=wav.getnchannels())
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(write=write_wav, info=info))
    monkeypatch.setattr(module, 'load_engine', lambda *args, **kwargs: Engine())
    module._run(tmp_path)
    progress = json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8'))
    assert progress['completed'] == ['0', '1']
    saved = [(tmp_path / 'assets' / (item['generation_hash'] + '.wav')).read_bytes() for item in items[:2]]
    (tmp_path / 'control.json').unlink()
    module._run(tmp_path)
    progress = json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8'))
    assert progress['completed'] == ['0', '1', '2']
    assert progress['failed'] == []
    assert calls == [['cue 0', 'cue 1'], ['cue 2']]
    assert saved == [(tmp_path / 'assets' / (item['generation_hash'] + '.wav')).read_bytes() for item in items[:2]]


def test_machine_profile_requires_matching_model_and_sdk(tmp_path, monkeypatch):
    module = worker_module()
    path = tmp_path / 'profile.json'
    monkeypatch.setenv('CONTENT_BOT_VOICE_PROFILE', str(path))
    monkeypatch.delenv('CONTENT_BOT_VOICE_BATCH_SIZE', raising=False)
    write_json(path, {'sdk': module.SDK, 'model_revision': 'old', 'cuda_batch_size': 4})
    assert module.batch_limit('cuda') == 2
    write_json(path, {'sdk': module.SDK, 'model_revision': module.REVISION, 'cuda_batch_size': 4})
    assert module.batch_limit('cuda') == 4


@pytest.mark.parametrize('damaged', [False, True])
def test_recover_checkpoint_avoids_model_and_rejects_corruption(tmp_path, damaged, monkeypatch):
    store = VoiceStore(tmp_path)
    doc = store.save_document('owner', VoiceDocument(
        project_id='a' * 20, video_fingerprint='video',
        profile={'id': 'voice', 'name': 'voice', 'preset': 'test'},
        clips=[VoiceClip(id='one', spoken_text='Xin chào', start_ms=0, end_ms=2000)]))
    key = generation_hash(doc, doc.clips[0], 'cpu')
    output = store.owner_root('owner') / 'work' / 'old' / 'assets'
    output.mkdir(parents=True)
    wav = output / f'{key}.wav'
    with wave.open(str(wav), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'none'))
        audio.writeframes(array.array('h', [1000] * 2400).tobytes())
    original = wav.read_bytes()
    meta = audio_metadata(wav)
    write_json(wav.with_suffix('.json'), {'generation_hash': key,
               'checksum': 'bad' if damaged else meta['checksum']})
    manager = VoiceManager(store)
    try:
        manager._recover_checkpoints('owner', manager.manifest('owner', doc, 'cpu'))
        dest = store.path('owner', 'assets', key, '.wav')
        assert dest.exists() is not damaged
        if not damaged:
            assert dest.read_bytes() == original
            monkeypatch.setattr(manager, 'status', lambda: {'ready': True, 'devices': ['cpu']})
            def no_model(*args):
                raise AssertionError('Recovered clips must not launch a model')
            monkeypatch.setattr(manager, 'python', no_model)
            job = manager.start('owner', doc.project_id, 'cpu')
            deadline = time.monotonic() + 5
            while job['state'] in {'queued', 'running'} and time.monotonic() < deadline:
                time.sleep(.02)
                job = manager.get('owner', job['id'])
            assert job['state'] == 'succeeded', job
            assert store.get_document('owner', doc.project_id).clips[0].asset_id == key
        assert wav.read_bytes() == original
    finally:
        manager.shutdown()


def test_bulk_attach_keeps_edited_lines_and_updates_document_once(tmp_path):
    store = VoiceStore(tmp_path)
    doc = store.save_document('owner', VoiceDocument(
        project_id='a' * 20, video_fingerprint='video',
        profile={'id': 'voice', 'name': 'voice', 'preset': 'test'},
        clips=[VoiceClip(id=str(i), spoken_text='Xin chào', start_ms=i*2000, end_ms=(i+1)*2000)
               for i in range(3)]))
    key = generation_hash(doc, doc.clips[0], 'cpu')
    meta = {'id': key, 'generation_hash': key, 'device': 'cpu', 'duration_ms': 1000}
    doc.clips[2].spoken_text = 'Đã sửa'
    doc = store.save_document('owner', doc)
    assert store.attach_many('owner', doc.project_id, {str(i): meta for i in range(3)}) == {'0', '1'}
    result = store.get_document('owner', doc.project_id)
    assert result.revision == doc.revision + 1
    assert result.clips[2].asset_id is None
