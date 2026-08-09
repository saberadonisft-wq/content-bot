from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas import SubtitleCueV2, SubtitleDocumentV2, SubtitleWordV2
from app.services.subtitle_timing import (
    SubtitleTimingError,
    ms_to_srt_time,
    parse_timestamp_to_ms,
    transform_project_cues,
    validate_cues,
)
from app.services.subtitles import (
    parse_subtitles_text,
    parse_subtitles_v2,
    subtitles_to_srt,
)


def test_timestamp_parser_and_formatter_preserve_milliseconds() -> None:
    assert parse_timestamp_to_ms("00:00:00.001") == 1
    assert parse_timestamp_to_ms("00:00:00,999") == 999
    assert parse_timestamp_to_ms("01:02:03.456") == 3_723_456
    assert parse_timestamp_to_ms("62:03.456") == 3_723_456
    assert parse_timestamp_to_ms("0.0005") == 1
    assert ms_to_srt_time(3_723_456) == "01:02:03,456"


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-0.1", "00:60.000", "00:00:60.000"]
)
def test_timestamp_parser_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(SubtitleTimingError):
        parse_timestamp_to_ms(value)


def test_v2_schema_requires_integer_milliseconds() -> None:
    with pytest.raises(ValidationError):
        SubtitleCueV2(id="cue-1", start_ms=1.5, end_ms=1000, text="Không hợp lệ")
    with pytest.raises(ValidationError):
        SubtitleWordV2(id="word-1", start_ms=0, end_ms=0, text="lỗi")


def test_parse_json_v2_preserves_ms_metadata_and_words() -> None:
    payload = {
        "schema_version": 2,
        "language": "vi",
        "timebase": "milliseconds",
        "timing_source": "gemini_estimate",
        "timing_precision_ms": 100,
        "segments": [
            {
                "id": "s0001",
                "start_ms": 75_230,
                "end_ms": 78_640,
                "text": "Ờ... vẫn chưa.",
                "confidence": 0.72,
                "words": [
                    {
                        "id": "s0001-w1",
                        "text": "Ờ",
                        "start_ms": 75_230,
                        "end_ms": 75_500,
                        "confidence": 0.8,
                    },
                    {
                        "id": "s0001-w2",
                        "text": "chưa",
                        "start_ms": 78_000,
                        "end_ms": 78_640,
                        "confidence": 0.7,
                    },
                ],
            }
        ],
    }
    document, warnings = parse_subtitles_v2(json.dumps(payload, ensure_ascii=False))
    validated = SubtitleDocumentV2(**document)

    assert warnings == []
    assert validated.segments[0].start_ms == 75_230
    assert validated.segments[0].end_ms == 78_640
    assert validated.segments[0].words is not None
    assert (
        subtitles_to_srt(document["segments"]).splitlines()[1]
        == "00:01:15,230 --> 00:01:18,640"
    )


def test_json_code_fence_and_legacy_array_remain_supported() -> None:
    raw = """```json
    [{"start": "00:00.001", "end": "00:01.234", "text": "Câu một"}]
    ```"""
    document, warnings = parse_subtitles_v2(raw)
    legacy = parse_subtitles_text(raw)

    assert warnings == []
    assert document["segments"][0]["start_ms"] == 1
    assert document["segments"][0]["end_ms"] == 1234
    assert legacy[0]["start_seconds"] == 0.001
    assert legacy[0]["end_time"] == "00:00:01,234"


def test_untrusted_json_metadata_is_sanitized_without_response_failure() -> None:
    payload = {
        "schema_version": 2,
        "language": "../../invalid",
        "timing_source": "invented",
        "timing_precision_ms": 0,
        "segments": [
            {
                "id": "unsafe cue id",
                "start_ms": 10,
                "end_ms": 1000,
                "text": "x" * 4100,
                "secondary_text": "y" * 4100,
                "timing_source": "unknown",
                "timing_precision_ms": 100_000,
                "revision": True,
                "words": [
                    {
                        "id": "outside",
                        "text": "ngoài cue",
                        "start_ms": 0,
                        "end_ms": 20,
                    },
                    {
                        "id": "inside",
                        "text": "z" * 600,
                        "start_ms": 20,
                        "end_ms": 30,
                    },
                ],
            }
        ],
    }

    document, warnings = parse_subtitles_v2(json.dumps(payload))
    validated = SubtitleDocumentV2(**document)
    cue = validated.segments[0]

    assert validated.language == "vi"
    assert validated.timing_source == "gemini_estimate"
    assert cue.timing_source == "gemini_estimate"
    assert cue.timing_precision_ms == 1
    assert cue.revision == 0
    assert len(cue.text) == 4000
    assert cue.secondary_text is not None and len(cue.secondary_text) == 4000
    assert cue.words is not None and len(cue.words) == 1
    assert len(cue.words[0].text) == 500
    warning_codes = {warning["code"] for warning in warnings}
    assert {
        "invalid_language",
        "invalid_timing_source",
        "invalid_timing_precision",
        "cue_text_truncated",
        "secondary_text_truncated",
        "word_outside_cue",
        "word_text_truncated",
    } <= warning_codes

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/subtitles/v2/parse",
            json={"text": json.dumps(payload)},
        )
    assert response.status_code == 200


def test_bracketed_srt_arrow_no_longer_crashes_parser() -> None:
    document, warnings = parse_subtitles_v2("[00:00:00.001 --> 00:00:01.234] Nội dung")

    assert warnings == []
    assert document["segments"][0]["start_ms"] == 1
    assert document["segments"][0]["end_ms"] == 1234


def test_ids_are_stable_and_overlap_is_reported_without_auto_shift() -> None:
    raw = """
    [00:00.000 - 00:02.000] Câu một
    [00:01.500 - 00:03.000] Câu hai
    """
    first, first_warnings = parse_subtitles_v2(raw)
    second, _ = parse_subtitles_v2(raw)

    assert [cue["id"] for cue in first["segments"]] == [
        cue["id"] for cue in second["segments"]
    ]
    assert first["segments"][1]["start_ms"] == 1500
    assert any(
        warning["code"] == "overlap" and warning["delta_ms"] == 500
        for warning in first_warnings
    )


def test_nested_overlap_is_not_hidden_by_a_short_middle_cue() -> None:
    warnings = validate_cues(
        [
            {"id": "long", "start_ms": 0, "end_ms": 10_000, "text": "Dài"},
            {"id": "short", "start_ms": 1000, "end_ms": 2000, "text": "Ngắn"},
            {"id": "nested", "start_ms": 3000, "end_ms": 4000, "text": "Lồng"},
        ]
    )

    nested = next(
        warning
        for warning in warnings
        if warning["code"] == "overlap" and warning["cue_id"] == "nested"
    )
    assert nested["related_cue_id"] == "long"
    assert nested["delta_ms"] == 7000


def test_time_map_trims_clamps_and_rounds_once() -> None:
    transformed = transform_project_cues(
        [
            {"id": "before", "start_ms": 0, "end_ms": 900, "text": "Bỏ"},
            {"id": "cross", "start_ms": 900, "end_ms": 2300, "text": "Giữ"},
            {"id": "after", "start_ms": 3100, "end_ms": 4000, "text": "Bỏ"},
        ],
        trim_start_ms=1000,
        trim_end_ms=3000,
        video_speed=Decimal("1.25"),
    )

    assert len(transformed) == 1
    assert transformed[0]["source_start_ms"] == 1000
    assert transformed[0]["source_end_ms"] == 2300
    assert transformed[0]["start_ms"] == 0
    assert transformed[0]["end_ms"] == 1040


def test_time_map_applies_to_word_timing() -> None:
    transformed = transform_project_cues(
        [
            {
                "id": "cue",
                "start_ms": 1000,
                "end_ms": 2000,
                "text": "xin chào",
                "words": [
                    {"id": "w1", "text": "xin", "start_ms": 1000, "end_ms": 1300},
                    {"id": "w2", "text": "chào", "start_ms": 1500, "end_ms": 2000},
                ],
            }
        ],
        trim_start_ms=500,
        video_speed=2,
    )

    assert transformed[0]["start_ms"] == 250
    assert transformed[0]["end_ms"] == 750
    assert transformed[0]["words"][0]["start_ms"] == 250
    assert transformed[0]["words"][1]["end_ms"] == 750


def test_srt_round_trip_keeps_ms() -> None:
    source = [{"id": "cue", "start_ms": 1, "end_ms": 999, "text": "Một mili giây"}]
    srt = subtitles_to_srt(source)
    parsed, warnings = parse_subtitles_v2(srt)

    assert warnings == []
    assert parsed["segments"][0]["start_ms"] == 1
    assert parsed["segments"][0]["end_ms"] == 999


def test_v2_parse_and_transform_api() -> None:
    payload = {
        "schema_version": 2,
        "language": "vi",
        "timebase": "milliseconds",
        "timing_source": "gemini_estimate",
        "timing_precision_ms": 1,
        "segments": [
            {"id": "s1", "start_ms": 1001, "end_ms": 2501, "text": "Câu kiểm thử"}
        ],
    }
    with TestClient(app) as client:
        parsed = client.post(
            "/api/v1/subtitles/v2/parse",
            json={
                "text": json.dumps(payload, ensure_ascii=False),
                "media_duration_ms": 5000,
            },
        )
        assert parsed.status_code == 200
        assert parsed.json()["document"]["segments"][0]["start_ms"] == 1001

        transformed = client.post(
            "/api/v1/subtitles/v2/transform",
            json={
                "document": parsed.json()["document"],
                "time_map": {
                    "trim_start_ms": 501,
                    "trim_end_ms": 3000,
                    "video_speed": "2",
                },
            },
        )

    assert transformed.status_code == 200
    cue = transformed.json()["document"]["segments"][0]
    assert cue["start_ms"] == 250
    assert cue["end_ms"] == 1000
