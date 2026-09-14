import hashlib
import threading
import wave

import numpy as np
import pytest

from app.services.voiceover.processed_audio import prepare_audio


def test_actual_trim_tempo_cache_and_corruption_recovery_preserve_original(tmp_path):
    source = tmp_path / 'original.wav'
    sample_rate = 16000
    tone = np.rint(7000 * np.sin(2 * np.pi * 300 * np.arange(16000) / sample_rate)).astype('<i2')
    with wave.open(str(source), 'wb') as wav:
        wav.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
        wav.writeframes(np.concatenate([np.zeros(3200, dtype='<i2'), tone, np.zeros(3200, dtype='<i2')]).tobytes())
    original = source.read_bytes()
    args = {'checksum': hashlib.sha256(original).hexdigest(), 'trim_start_ms': 100,
            'trim_end_ms': 100, 'rate': 1.1, 'cache_dir': tmp_path / 'processed'}
    first = prepare_audio(source, **args)
    assert not first['cache_hit'] and first['playback_rate'] == 1
    assert abs(first['duration_ms'] - 1200 / 1.1) < 60
    assert first['output_measured'] and not first['output_speech_verified']
    second = prepare_audio(source, **args)
    assert second['cache_hit'] and second['checksum'] == first['checksum']
    output = args['cache_dir'] / first['filename']
    output.write_bytes(b'broken cache file')
    repaired = prepare_audio(source, **args)
    assert not repaired['cache_hit'] and repaired['checksum'] == first['checksum']
    assert source.read_bytes() == original
    with pytest.raises(ValueError):
        prepare_audio(source, **{**args, 'checksum': 'a' * 64})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        prepare_audio(source, **args, cancel=cancel)
