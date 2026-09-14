import threading

import numpy as np
import pytest

from app.services.voiceover import source_activity as module


class FakeVAD:
    loads = 0

    def __init__(self):
        type(self).loads += 1

    def reset(self):
        pass

    def feed(self, samples, final=False):
        return [(ms, 1 if 128 <= ms < 512 else 0) for ms in range(0, 1000, 32)]


def test_window_budget_cache_and_gap_classification(tmp_path, monkeypatch):
    video = tmp_path / 'video'
    video.write_bytes(b'original')
    media = {'fingerprint': 'a', 'duration_ms': 10000, 'has_audio': True}
    calls = []
    monkeypatch.setattr(module, 'SileroStream', FakeVAD)
    monkeypatch.setattr(module, '_extract_pcm_window', lambda *args, **kwargs:
                        calls.append(args[1]) or np.zeros(16000, dtype='<i2').tobytes())
    options = {'cache_dir': tmp_path / 'cache', 'max_audio_ms': 2000}
    result = module.observe_source_activity(video, media, [(0, 1000), (2000, 3000), (5000, 6000)], **options)
    assert len(calls) == 2 and FakeVAD.loads == 1
    assert len(result['skipped']) == 1
    assert module.classify_gap(100, 400, result) == 'speech_in_gap_candidate'
    assert module.classify_gap(800, 900, result) == 'quiet_gap_candidate'
    assert module.classify_gap(900, 2100, result) == 'unobserved_gap'
    assert all(not w['automatic_borrow_allowed'] for w in result['windows'])
    cached = module.observe_source_activity(video, media, [(0, 1000), (2000, 3000)], **options)
    assert all(w['cache_hit'] for w in cached['windows']) and len(calls) == 2
    video.write_bytes(b'changed media bytes')
    module.observe_source_activity(video, media, [(0, 1000)], **options)
    assert len(calls) == 3
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        module.observe_source_activity(video, media, [(0, 1000)], cancel=cancel, **options)
