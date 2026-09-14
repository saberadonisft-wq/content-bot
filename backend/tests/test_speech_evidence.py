import json
from copy import deepcopy
from pathlib import Path

from app.schemas import SubtitleDocumentV2
from app.services.speech_evidence import valid_speech_evidence
from app.services.subtitle_alignment import (
    AlignmentSettings,
    ObservedWord,
    align_subtitle_document,
)
from app.services.subtitles import parse_subtitles_v2


def test_screen_text_keeps_display_while_speech_is_aligned_outside_it(monkeypatch):
    from app.services import subtitle_alignment as module
    source = {'id': 'a', 'start_ms': 1000, 'end_ms': 2000, 'text': 'Xin chào',
              'source_text': 'Hello world', 'source_language': 'en', 'content_source': 'screen',
              'timing_source': 'gemini_estimate', 'timing_precision_ms': 100, 'revision': 0}
    doc = {'schema_version': 2, 'language': 'vi', 'segments': [source]}
    before = deepcopy(doc)
    media = {'has_audio': True, 'duration_ms': 5000, 'fingerprint': 'video-a'}
    monkeypatch.setattr(module, '_resolve_engine', lambda _: 'faster_whisper')
    monkeypatch.setattr(module, '_load_whisper_model', lambda _: object())
    monkeypatch.setattr(module, '_extract_pcm_window', lambda *a, **k: b'pcm')
    monkeypatch.setattr(module, '_transcribe_pcm', lambda *a, **k: [
        ObservedWord('Hello', 800, 1200, .95), ObservedWord('world', 1200, 2100, .95)])
    result = align_subtitle_document(Path('unused'), doc, media, settings=AlignmentSettings(engine='faster_whisper'))
    cue = result['document']['segments'][0]
    assert doc == before
    assert (cue['start_ms'], cue['end_ms']) == (1000, 2000)
    assert (cue['speech_start_ms'], cue['speech_end_ms']) == (800, 2100)
    assert cue['timing_source'] == 'gemini_estimate'
    assert cue['speech_evidence']['method'] == 'asr_observed'
    assert all(w['alignment_method'] == 'asr_observed' for w in cue['words'])
    assert valid_speech_evidence(cue, media)
    assert not valid_speech_evidence(cue, {**media, 'fingerprint': 'different-video'})
    assert not valid_speech_evidence({**cue, 'source_text': 'Changed words'}, media)
    assert not valid_speech_evidence({**cue, 'speech_end_ms': 2200}, media)
    validated = SubtitleDocumentV2.model_validate(result['document'])
    parsed, warnings = parse_subtitles_v2(validated.model_dump_json(), media_duration_ms=5000)
    assert not warnings
    assert parsed['segments'][0]['speech_evidence'] == cue['speech_evidence']
    assert parsed['segments'][0]['words'][0]['start_ms'] == 800


def test_unknown_legacy_alignment_and_imported_stale_evidence_stay_unverified():
    assert valid_speech_evidence({'timing_source': 'forced_alignment', 'speech_start_ms': 0, 'speech_end_ms': 100}) is None
    payload = {'segments': [{'id': 'a', 'start_ms': 100, 'end_ms': 200, 'text': 'Hello',
                            'speech_start_ms': 0, 'speech_end_ms': 300,
                            'speech_evidence': {'method': 'manual'}}]}
    parsed, _ = parse_subtitles_v2(json.dumps(payload), media_duration_ms=250)
    assert 'speech_evidence' not in parsed['segments'][0]
    assert 'speech_start_ms' not in parsed['segments'][0]


def test_preserve_display_changes_cache_key():
    from app.services.subtitle_alignment import alignment_cache_key
    assert alignment_cache_key({}, {}, AlignmentSettings()) != alignment_cache_key(
        {}, {}, AlignmentSettings(preserve_display=True))
