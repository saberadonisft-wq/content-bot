import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path


def worker_module():
    path = Path(__file__).resolve().parents[1] / 'worker.py'
    spec = importlib.util.spec_from_file_location('voice_worker_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(worker, root):
    worker.write(root / 'manifest.json', {
        'schema_version': 1, 'sdk_version': worker.SDK, 'device': 'cpu',
        'profile': {'model_revision': worker.REVISION, 'preset': 'test'},
        'clips': [{'id': 'one', 'generation_hash': 'a' * 64, 'text': 'hello'}],
    })


def test_paused_worker_does_not_load_model(tmp_path, monkeypatch):
    worker = worker_module()
    manifest(worker, tmp_path)
    worker.write(tmp_path / 'control.json', {'action': 'pause'})
    def forbidden(*args, **kwargs):
        raise AssertionError('paused worker loaded model')
    monkeypatch.setattr(worker, 'load_engine', forbidden)
    worker.run(tmp_path)
    assert json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8'))['message'] == 'Đã dừng theo yêu cầu'


def test_prepare_rejects_silence_and_recovers_corrupt_status(tmp_path, monkeypatch):
    import numpy as np
    import pytest
    worker = worker_module()
    monkeypatch.setattr(worker, '__file__', str(tmp_path / 'worker.py'))
    (tmp_path / 'runtime-status.json').write_text('{invalid', encoding='utf-8')
    class Engine:
        sample_rate = 48000
        silent = True
        def infer(self, *args, **kwargs):
            return np.zeros(4800) if self.silent else np.ones(4800) * 0.1
        def list_preset_voices(self):
            return [('Sample', 'sample')]
    engine = Engine()
    monkeypatch.setattr(worker, 'load_engine', lambda *args, **kwargs: engine)
    with pytest.raises(ValueError, match='runtime'):
        worker.prepare('cpu')
    engine.silent = False
    worker.prepare('cpu')
    status = json.loads((tmp_path / 'runtime-status.json').read_text(encoding='utf-8'))
    assert status['ready'] is True
    assert status['devices'] == ['cpu']


def test_corrupt_checkpoint_regenerates_and_valid_checkpoint_skips(tmp_path, monkeypatch):
    import numpy as np
    worker = worker_module()
    manifest(worker, tmp_path)
    calls = []
    class Engine:
        sample_rate = 48000
        def infer(self, text, **kwargs):
            calls.append(text)
            return np.ones(4800, dtype=np.float32) * 0.1
    monkeypatch.setattr(worker, 'load_engine', lambda *args, **kwargs: Engine())
    output = tmp_path / 'assets'
    output.mkdir()
    (output / ('a' * 64 + '.wav')).write_bytes(b'broken')
    (output / ('a' * 64 + '.json')).write_text('{invalid', encoding='utf-8')
    worker.run(tmp_path)
    worker.run(tmp_path)
    assert calls == ['hello']
    progress = json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8'))
    assert progress['completed'] == ['one']
    assert progress['failed'] == []


def test_cuda_oom_retries_once_without_changing_voice_or_device(monkeypatch):
    worker = worker_module()
    calls, cleared = [], []
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: cleared.append(True))))
    class Engine:
        def infer(self, text, **kwargs):
            calls.append((text, kwargs))
            if len(calls) == 1:
                raise RuntimeError('CUDA out of memory')
            return 'audio'
    assert worker.infer_with_retry(Engine(), 'hello', {'voice': 'sample'}, 'cuda') == 'audio'
    assert calls[0] == calls[1]
    assert cleared == [True]


def test_repeated_oom_has_actionable_error_and_other_failures_are_not_retried(monkeypatch):
    import pytest
    worker = worker_module()
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: None)))
    calls = []
    class Engine:
        def infer(self, text, **kwargs):
            calls.append(text)
            raise RuntimeError(text)
    with pytest.raises(RuntimeError, match='Kaggle'):
        worker.infer_with_retry(Engine(), 'CUDA out of memory', {}, 'cuda')
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(RuntimeError, match='invalid voice'):
        worker.infer_with_retry(Engine(), 'invalid voice', {}, 'cuda')
    assert len(calls) == 1
