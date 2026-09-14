import sys
from types import ModuleType

from huggingface_hub.errors import LocalEntryNotFoundError

from app.services.subtitle_alignment import AlignmentSettings, _load_whisper_model


def test_offline_alignment_reuses_shared_cache_without_loading_or_downloading(tmp_path, monkeypatch):
    calls = []
    whisper = ModuleType('faster_whisper')
    utils = ModuleType('faster_whisper.utils')

    def download(model, **kwargs):
        calls.append((model, kwargs))
        assert kwargs['local_files_only'] is True
        if kwargs.get('cache_dir'):
            raise LocalEntryNotFoundError('Not in project cache')
        return '/cached/snapshot/model'

    utils.download_model = download
    whisper.WhisperModel = lambda model, **kwargs: (model, kwargs)
    monkeypatch.setitem(sys.modules, 'faster_whisper', whisper)
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', utils)
    model, options = _load_whisper_model(AlignmentSettings(whisper_model_dir=tmp_path / 'empty',
        whisper_device='cpu', whisper_compute_type='int8', cpu_threads=3))
    assert model == '/cached/snapshot/model' and len(calls) == 2
    assert options['device'] == 'cpu' and options['cpu_threads'] == 3 and options['local_files_only']
    assert not (tmp_path / 'empty').exists()


def test_observation_cache_rechecks_new_transcript_without_inference_and_invalidates_model_audio(tmp_path, monkeypatch):
    import app.services.subtitle_alignment as module

    model = tmp_path / 'model'
    model.mkdir()
    weights = model / 'model.bin'
    weights.write_bytes(b'weights')
    monkeypatch.setattr(module, '_resolve_engine', lambda _: 'faster_whisper')
    monkeypatch.setattr(module, '_resolve_whisper_model_path', lambda _: str(model))
    loads, recognitions = [], []
    monkeypatch.setattr(module, '_load_whisper_model', lambda settings: loads.append(settings) or object())
    monkeypatch.setattr(module, '_extract_pcm_window', lambda *a, **kw: b'pcm')

    def transcribe(*args):
        recognitions.append(True)
        return [module.ObservedWord('你好', 1100, 2200, .95)]

    monkeypatch.setattr(module, '_transcribe_pcm', transcribe)
    cue = {'id': 'cue', 'start_ms': 1000, 'end_ms': 2500, 'text': 'Xin chào', 'source_text': '你好世界',
           'source_language': 'zh', 'timing_source': 'gemini_estimate', 'revision': 0}
    media = {'has_audio': True, 'duration_ms': 5000, 'audio_hash': 'one'}
    settings = AlignmentSettings(engine='faster_whisper', preserve_display=True)

    def run():
        return module.align_subtitle_document(tmp_path / 'video', {'language': 'vi', 'segments': [cue]},
            media, settings=settings, cache_dir=tmp_path / 'cache')

    first = run()
    assert first['aligned_cue_count'] == 0
    assert not first['source_observations'][0]['cache_hit']
    cue['source_text'] = '你好'
    cue['id'] = 'edited-cue'
    second = run()
    assert second['aligned_cue_count'] == 1
    assert second['document']['segments'][0]['id'] == 'edited-cue'
    assert second['source_observations'][0]['cache_hit']
    assert len(loads) == len(recognitions) == 1
    assert run()['cache_hit']
    weights.write_bytes(b'different-model-weights')
    assert not run()['source_observations'][0]['cache_hit']
    assert len(loads) == len(recognitions) == 2
    # Audio changes must miss even when the window bounds and spoken words are unchanged.
    media['audio_hash'] = 'two'
    monkeypatch.setattr(module, '_extract_pcm_window', lambda *a, **kw: b'changed-pcm')
    assert not run()['source_observations'][0]['cache_hit']
    assert len(loads) == len(recognitions) == 3
