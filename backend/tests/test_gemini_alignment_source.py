from copy import deepcopy
from pathlib import Path

import pytest

from app.services.subtitle_alignment import (
    AlignmentSettings,
    ObservedWord,
    _align_whisper_window,
    align_subtitle_document,
    alignment_cache_key,
    transcript_tokens,
)


def cue(cue_id="a", **kwargs):
    return {"id": cue_id, "start_ms": 1000, "end_ms": 2500, "text": "Xin chào thế giới",
            "source_text": "你好世界", "source_language": "zh", "content_source": "audio",
            "timing_source": "gemini_estimate", "timing_precision_ms": 100, "revision": 0,
            "needs_review": False, **kwargs}


def test_cjk_uses_source_phrase_and_observed_word_spans():
    original = cue()
    aligned, warnings = _align_whisper_window([original], [ObservedWord("你好", 1100, 1600, 0.9), ObservedWord("世界", 1700, 2200, 0.95)])
    value = aligned["a"]
    assert value["text"] == original["text"] and value["source_text"] == original["source_text"]
    assert (value["start_ms"], value["end_ms"]) == (1100, 2200)
    assert value["words"] is None
    assert not warnings


def test_cjk_partial_asr_match_does_not_truncate_original_cue():
    original = cue()
    aligned, warnings = _align_whisper_window([original], [ObservedWord("你好", 1100, 1600, 0.9)])
    assert (aligned["a"]["start_ms"], aligned["a"]["end_ms"]) == (1000, 2500)
    assert aligned["a"]["needs_review"] and warnings
    assert warnings[0]['diagnostics']['matched_units'] == 2
    assert warnings[0]['diagnostics']['expected_units'] == 4
    assert warnings[0]['diagnostics']['observed_text'] == '你好'


@pytest.mark.parametrize(('source', 'words', 'reason'), [
    ('你好', [ObservedWord('你好世界', 1000, 2300, .99)], 'complete_word_boundaries'),
    ('你好世界', [ObservedWord('你好', 1000, 1300, .99), ObservedWord('美丽', 1400, 1600, .99),
                ObservedWord('世界', 1700, 2300, .99)], 'contiguous_match'),
    ('你好', [ObservedWord('你好', 1000, 1300, .99), ObservedWord('你好', 1700, 2300, .99)], 'ambiguous_occurrence'),
])
def test_matching_characters_cannot_claim_unobserved_or_ambiguous_boundaries(source, words, reason):
    original = cue(source_text=source)
    aligned, warnings = _align_whisper_window([original], words)
    assert aligned['a']['start_ms'] == original['start_ms']
    assert aligned['a']['end_ms'] == original['end_ms']
    assert aligned['a']['needs_review']
    assert warnings[0]['diagnostics'][reason] is (reason == 'ambiguous_occurrence')


def test_source_used_even_if_bilingual_hidden():
    assert [token.text for token in transcript_tokens(cue(source_text="Hello world", secondary_text=None))] == ["Hello", "world"]


def test_alignment_groups_source_languages_and_never_changes_locked_or_outside_scope(tmp_path, monkeypatch):
    import app.services.subtitle_alignment as module
    first = cue()
    second = cue("b", source_text="こんにちは", source_language="ja", start_ms=3000, end_ms=4500)
    locked = cue("locked", locked=True, speech_start_ms=5100, speech_end_ms=5800, start_ms=5000, end_ms=6000)
    outside = cue("outside", speech_start_ms=7100, speech_end_ms=7800, start_ms=7000, end_ms=8000)
    doc = {"schema_version": 2, "language": "vi", "segments": [first, second, locked, outside]}
    before = deepcopy(doc)
    languages = []
    monkeypatch.setattr(module, "_resolve_engine", lambda value: "faster_whisper")
    monkeypatch.setattr(module, "_load_whisper_model", lambda settings: object())
    monkeypatch.setattr(module, "_extract_pcm_window", lambda *a, **k: b"pcm")
    def transcribe(pcm, window, text, settings, model):
        languages.append((settings.source_language, text))
        return [ObservedWord(text, window.start_ms + 700, window.end_ms - 700, 0.95)]
    monkeypatch.setattr(module, "_transcribe_pcm", transcribe)
    result = align_subtitle_document(Path("unused"), doc, {"has_audio": True, "duration_ms": 9000}, settings=AlignmentSettings(engine="faster_whisper", force_manual=True), cue_ids={"a", "b", "locked"})
    assert languages == [("zh", "你好世界"), ("ja", "こんにちは")]
    assert result['source_observations'][0]['words'][0]['text'] == '你好世界'
    assert result["document"]["segments"][2:] == before["segments"][2:]
    assert doc == before
    assert [c["text"] for c in result["document"]["segments"]] == [c["text"] for c in before["segments"]]


def test_empty_selection_and_cache_source_changes(tmp_path, monkeypatch):
    import app.services.subtitle_alignment as module
    doc = {"language": "vi", "segments": [cue()]}
    media = {"has_audio": True, "duration_ms": 3000, "audio_hash": "abc"}
    monkeypatch.setattr(module, "_extract_pcm_window", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not extract")))
    result = align_subtitle_document(Path("unused"), doc, media, settings=AlignmentSettings(engine="energy"), cue_ids=set())
    assert result["document"] == doc and result["aligned_cue_count"] == 0
    key = alignment_cache_key(doc, media, AlignmentSettings())
    assert key != alignment_cache_key({**doc, "segments": [cue(source_text="新的文字")]}, media, AlignmentSettings())
    assert key != alignment_cache_key(doc, media, AlignmentSettings(), set())


def test_padding_tolerates_explicit_null_speech_fields():
    from app.services.subtitle_alignment import apply_display_padding
    cues = [cue(speech_start_ms=None, speech_end_ms=None), cue("b", speech_start_ms=None, speech_end_ms=None)]
    assert len(apply_display_padding(cues, 5000, lead_in_ms=60, tail_ms=100)) == 2


def test_long_cue_does_not_bypass_alignment_window_resource_limit(monkeypatch):
    import app.services.subtitle_alignment as module
    original = cue(end_ms=90000)
    document = {"language": "vi", "segments": [original]}
    monkeypatch.setattr(module, "_extract_pcm_window", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not extract")))
    result = align_subtitle_document(Path("unused"), document, {"has_audio": True, "duration_ms": 100000}, settings=AlignmentSettings(engine="energy"))
    assert result["document"] == document
    assert result["warnings"][0]["code"] == "alignment_window_limit"


def test_low_confidence_alignment_keeps_timing_and_marks_review(monkeypatch):
    import app.services.subtitle_alignment as module
    original = cue(source_text="Hello world", source_language="en")
    document = {"language": "vi", "segments": [original]}
    monkeypatch.setattr(module, "_resolve_engine", lambda value: "faster_whisper")
    monkeypatch.setattr(module, "_load_whisper_model", lambda settings: object())
    monkeypatch.setattr(module, "_extract_pcm_window", lambda *a, **k: b"pcm")
    monkeypatch.setattr(module, "_transcribe_pcm", lambda *a, **k: [ObservedWord("Hello", 1100, 1500, 0.2), ObservedWord("world", 1500, 2000, 0.2)])
    result = align_subtitle_document(Path("unused"), document, {"has_audio": True, "duration_ms": 5000}, settings=AlignmentSettings(engine="faster_whisper"))
    actual = result["document"]["segments"][0]
    assert (actual["start_ms"], actual["end_ms"]) == (original["start_ms"], original["end_ms"])
    assert actual["needs_review"] and result["aligned_cue_count"] == 0
