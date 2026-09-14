from pathlib import Path

import pytest

from app.services.speech_evidence import SpeechEvidence, audio_identity, transcript_hash
from app.services.voiceover import dubbed_speech as module


def fake_analysis(monkeypatch, *, heard='Tôi không đi', confidence=.95, vad_start=220):
    checks = []

    def quiet(*args, **kwargs):
        checks.append(kwargs.get('expected_checksum'))
        return {'duration_ms': 1500, 'has_detectable_signal': True,
                'protected_head_candidate_ms': 190, 'protected_tail_candidate_ms': 240}

    monkeypatch.setattr(module, 'analyze_quiet_edges', quiet)
    monkeypatch.setattr(module, 'observe_source_activity', lambda *args, **kw: {
        'windows': [{'start_ms': 0, 'end_ms': 1500, 'speech': [[vad_start, 1260]]}], 'skipped': []})

    def align(path, document, media, **kwargs):
        assert kwargs['settings'].whisper_device == 'cpu'
        assert not kwargs['settings'].whisper_allow_download
        cue = document['segments'][0]
        assert cue['source_text'] == 'Tôi không đi' and cue['source_language'] == 'vi'
        cue['speech_evidence'] = SpeechEvidence(method='asr_observed', audio_identity=audio_identity(media),
            transcript_sha256=transcript_hash(cue), start_ms=250, end_ms=1200,
            transcript_complete=True, algorithm='fixture').model_dump()
        cue.update(speech_start_ms=250, speech_end_ms=1200, needs_review=False)
        return {'document': document, 'warnings': [], 'source_observations': [
            {'words': [{'text': heard, 'start_ms': 250, 'end_ms': 1200, 'confidence': confidence}]}]}

    monkeypatch.setattr(module, 'align_subtitle_document', align)
    return checks


def test_edges_require_transcript_activity_and_energy_and_preserve_guard(tmp_path, monkeypatch):
    checks = fake_analysis(monkeypatch)
    result = module.inspect_dubbed_speech(Path('unused'), 'Tôi không đi', checksum='a' * 64,
        model_dir=tmp_path, cache_dir=tmp_path)
    assert result['automatic_trim_allowed'] and result['speech_verified']
    assert result['trim_start_ms'] == 140 and result['trim_end_ms'] == 160
    assert not result['human_verified']
    assert checks == ['a' * 64, 'a' * 64]


@pytest.mark.parametrize(('heard', 'confidence'), [('Tôi đi', .99), ('Tôi không đi đi', .99), ('Tôi không đi', .4)])
def test_lost_negation_repetition_and_uncertain_words_do_not_authorize_trim(tmp_path, monkeypatch, heard, confidence):
    fake_analysis(monkeypatch, heard=heard, confidence=confidence)
    result = module.inspect_dubbed_speech(Path('unused'), 'Tôi không đi', checksum='a' * 64,
        model_dir=tmp_path, cache_dir=tmp_path)
    assert result['issues'] == ['dubbed_transcript_unverified']
    assert not result['automatic_trim_allowed'] and not result['speech_verified']
    assert result['trim_start_ms'] == result['trim_end_ms'] == 0
    assert result['diagnostics']['transcript_matches'] == (heard == 'Tôi không đi')
    assert result['diagnostics']['low_confidence_words'] == ([heard] if confidence < .65 else [])


def test_faint_leading_activity_is_protected_even_when_asr_starts_later(tmp_path, monkeypatch):
    fake_analysis(monkeypatch, vad_start=35)
    result = module.inspect_dubbed_speech(Path('unused'), 'Tôi không đi', checksum='a' * 64,
        model_dir=tmp_path, cache_dir=tmp_path)
    assert result['trim_start_ms'] == 0
