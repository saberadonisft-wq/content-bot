import struct

import pytest

from app.services.voiceover import source_pause as module


@pytest.mark.parametrize('mode', ['quiet', 'vad_speech', 'quiet_but_weak_sound', 'semantic_pause', 'uncertain', 'short'])
def test_borrow_requires_context_vad_and_peak_protected_quiet(tmp_path, monkeypatch, mode):
    context = {'decision': {'source_is_clear': True, 'confidence': .95,
                           'tail_pause_has_no_semantic_function': mode != 'semantic_pause',
                           'no_scene_or_speaker_change_at_tail': True}}
    if mode == 'uncertain':
        context['decision']['confidence'] = .7
    observed = []

    def activity(video, media, windows, **kwargs):
        observed.append(windows)
        start, end = windows[0]
        return {'windows': [{'start_ms': start, 'end_ms': end,
                             'speech': [[start, end]] if mode == 'vad_speech' else []}]}

    def extract(video, window, **kwargs):
        value = 64 if mode == 'quiet_but_weak_sound' else 0  # about -54 dBFS: quiet, still above peak guard
        return struct.pack('<h', value) * ((window.end_ms - window.start_ms) * 16)

    monkeypatch.setattr(module, 'observe_source_activity', activity)
    monkeypatch.setattr(module, '_extract_pcm_window', extract)
    next_start = 1040 if mode == 'short' else 1600
    result = module.verified_tail(tmp_path / 'unused', {'duration_ms': 3000}, 1000, next_start, context,
                                  cache_dir=tmp_path / 'pause')
    assert result['allowed_ms'] == (250 if mode == 'quiet' else 0)
    if mode in {'semantic_pause', 'uncertain', 'short'}:
        assert not observed
    if mode == 'quiet':
        assert result['guard_ms'] == 80 and observed == [[(1000, 1330)]]
