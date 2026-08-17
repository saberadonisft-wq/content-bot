from __future__ import annotations

from array import array

from app.services.subtitle_alignment import (
    AlignmentSettings,
    AudioWindow,
    ObservedWord,
    TranscriptToken,
    _align_energy_window,
    _quantize_word_timings,
    apply_display_padding,
    build_audio_windows,
    match_transcript_words,
)


def _cue(cue_id: str, start_ms: int, end_ms: int, text: str) -> dict:
    return {
        "id": cue_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "timing_source": "gemini_estimate",
        "timing_precision_ms": 1000,
        "confidence": 0.8,
        "needs_review": False,
        "revision": 0,
    }


def test_word_match_ignores_punctuation_and_keeps_order() -> None:
    expected = [
        TranscriptToken("s1", 0, "Xin", "xin"),
        TranscriptToken("s1", 1, "chào", "chào"),
        TranscriptToken("s1", 2, "bạn", "bạn"),
    ]
    observed = [
        ObservedWord("xin,", 100, 300, 0.9),
        ObservedWord("chao", 310, 600, 0.8),
        ObservedWord("bạn!", 620, 900, 0.95),
    ]

    matches = match_transcript_words(expected, observed)

    assert [matches[index][0] for index in sorted(matches)] == [0, 1, 2]
    assert matches[0][1] == 1.0
    assert matches[2][1] == 1.0


def test_forced_alignment_word_timings_use_ten_ms_resolution() -> None:
    words = _quantize_word_timings(
        [
            {"id": "w1", "start_ms": 103, "end_ms": 207},
            {"id": "w2", "start_ms": 208, "end_ms": 259},
        ]
    )

    assert [(word["start_ms"], word["end_ms"]) for word in words] == [
        (100, 210),
        (210, 260),
    ]


def test_audio_windows_are_chunked_and_do_not_span_large_silence() -> None:
    windows = build_audio_windows(
        [
            _cue("a", 1_000, 2_000, "a"),
            _cue("b", 2_100, 3_000, "b"),
            _cue("c", 20_000, 21_000, "c"),
        ],
        30_000,
        padding_ms=500,
        max_window_ms=10_000,
    )

    assert [window.cue_ids for window in windows] == [("a", "b"), ("c",)]
    assert windows[0].start_ms == 500
    assert windows[0].end_ms == 3500


def test_energy_alignment_refines_speech_boundary_at_ten_ms_resolution() -> None:
    sample_count = 3 * 16_000
    samples = array("h", [0]) * sample_count
    for index in range(700 * 16, 1_230 * 16):
        samples[index] = 4_000 if index % 2 else -4_000
    cue = _cue("s1", 500, 1_500, "Xin chào bạn")

    aligned, warnings = _align_energy_window(
        [cue],
        [cue],
        AudioWindow(0, 3_000, ("s1",)),
        samples.tobytes(),
        3_000,
        # Defaults intentionally exercise the production threshold and padding.
        AlignmentSettings(),
    )

    result = aligned["s1"]
    assert warnings == []
    assert result["speech_start_ms"] == 700
    assert result["speech_end_ms"] == 1230
    assert result["timing_precision_ms"] == 10
    assert result["timing_source"] == "forced_alignment"
    assert result["words"][0]["start_ms"] >= result["speech_start_ms"]
    assert result["words"][-1]["end_ms"] == result["speech_end_ms"]


def test_display_padding_stops_at_midpoint_without_cutting_speech() -> None:
    left = {
        **_cue("left", 1_000, 2_000, "trái"),
        "speech_start_ms": 1_000,
        "speech_end_ms": 2_000,
    }
    right = {
        **_cue("right", 2_100, 3_000, "phải"),
        "speech_start_ms": 2_100,
        "speech_end_ms": 3_000,
    }

    padded = apply_display_padding(
        [left, right],
        5_000,
        lead_in_ms=100,
        tail_ms=200,
    )

    assert padded[0]["end_ms"] == 2050
    assert padded[1]["start_ms"] == 2050
    assert padded[0]["end_ms"] >= padded[0]["speech_end_ms"]
    assert padded[1]["start_ms"] <= padded[1]["speech_start_ms"]
