import json
from unittest.mock import MagicMock

import pytest

from app.schemas import SubtitleCueV2, SubtitleDocumentV2
from app.services.subtitle_translate import (
    SubtitleTranslateError,
    _validate_batch_translations,
    build_batch_translation_prompt,
    translate_source_document,
)


def test_validate_batch_translations_success():
    expected = ["c001", "c002", "c003"]
    returned = [
        {"id": "c001", "translation": "Xin chào"},
        {"id": "c002", "translation": "Hẹn gặp lại"},
        {"id": "c003", "translation": "Cảm ơn"},
    ]
    mapping = _validate_batch_translations(expected, returned)
    assert mapping == {
        "c001": "Xin chào",
        "c002": "Hẹn gặp lại",
        "c003": "Cảm ơn",
    }


def test_validate_batch_translations_missing_id():
    expected = ["c001", "c002", "c003"]
    # c002 is missing from Gemini's response
    returned = [
        {"id": "c001", "translation": "Xin chào"},
        {"id": "c003", "translation": "Cảm ơn"},
    ]
    with pytest.raises(SubtitleTranslateError, match="thiếu 1 đoạn"):
        _validate_batch_translations(expected, returned)


def test_validate_batch_translations_empty_translation():
    expected = ["c001", "c002"]
    # c002 has empty translation string
    returned = [
        {"id": "c001", "translation": "Xin chào"},
        {"id": "c002", "translation": ""},
    ]
    with pytest.raises(SubtitleTranslateError, match="thiếu 1 đoạn"):
        _validate_batch_translations(expected, returned)


def test_build_batch_translation_prompt_contains_data_and_context():
    batch = [{"id": "c1", "text": "Hello world"}, {"id": "c2", "text": "Goodbye"}]
    context_recent = [{"source": "Welcome", "translation": "Chào mừng"}]

    prompt = build_batch_translation_prompt(batch, context_recent, target_language="vi")

    assert "Chào mừng" in prompt
    assert '"c1"' in prompt
    assert '"c2"' in prompt
    assert "Hello world" in prompt
    assert "DATA" in prompt


def test_translate_source_document_preserves_timeline():
    # Setup document with 2 OCR source cues
    cues = [
        SubtitleCueV2(
            id="ocr_0001",
            start_ms=1200,
            end_ms=2500,
            text="你好世界",
            source_text="你好世界",
            source_language="zh",
            content_source="screen",
            timing_source="ocr",
            timing_precision_ms=100,
            confidence=0.95,
            needs_review=False,
            revision=0,
        ),
        SubtitleCueV2(
            id="ocr_0002",
            start_ms=2800,
            end_ms=4500,
            text="明天见",
            source_text="明天见",
            source_language="zh",
            content_source="screen",
            timing_source="ocr",
            timing_precision_ms=100,
            confidence=0.92,
            needs_review=False,
            revision=0,
        ),
    ]

    doc = SubtitleDocumentV2(
        schema_version=2,
        revision=0,
        language="zh",
        timing_source="ocr",
        timing_precision_ms=100,
        segments=cues,
    )

    # Mock service
    service = MagicMock()
    service.runtime_keys.return_value = [{"id": "k1", "enabled": True, "secret": "AIzaFake"}]
    service.resolve_model.return_value = "gemini-3.6-flash"
    service.settings.max_retries = 1

    # Lease context manager
    lease_cm = MagicMock()
    lease_cm.__enter__.return_value = {"id": "k1", "secret": "AIzaFake", "leased_model": "gemini-3.6-flash"}
    service.dispatcher.lease.return_value = lease_cm

    # Mock _generate_content_direct returning JSON
    gemini_resp = json.dumps({
        "translations": [
            {"id": "ocr_0001", "translation": "Xin chào thế giới"},
            {"id": "ocr_0002", "translation": "Hẹn gặp lại ngày mai"},
        ]
    })
    service._generate_content_direct.return_value = gemini_resp

    result = translate_source_document(
        service,
        doc,
        target_language="vi",
        bilingual=True,
    )

    trans_doc = result["document"]
    assert trans_doc["language"] == "vi"
    assert result["translated_count"] == 2

    segs = trans_doc["segments"]
    assert len(segs) == 2

    # Seg 1: timeline unchanged!
    assert segs[0]["id"] == "ocr_0001"
    assert segs[0]["start_ms"] == 1200
    assert segs[0]["end_ms"] == 2500
    assert segs[0]["text"] == "Xin chào thế giới"
    assert segs[0]["source_text"] == "你好世界"
    assert segs[0]["secondary_text"] == "你好世界"
    assert segs[0]["timing_source"] == "ocr"

    # Seg 2: timeline unchanged!
    assert segs[1]["id"] == "ocr_0002"
    assert segs[1]["start_ms"] == 2800
    assert segs[1]["end_ms"] == 4500
    assert segs[1]["text"] == "Hẹn gặp lại ngày mai"
    assert segs[1]["source_text"] == "明天见"
    assert segs[1]["secondary_text"] == "明天见"


def test_translate_source_document_no_keys():
    doc = SubtitleDocumentV2(
        schema_version=2,
        revision=0,
        language="zh",
        timing_source="ocr",
        timing_precision_ms=100,
        segments=[
            SubtitleCueV2(id="c1", start_ms=0, end_ms=1000, text="test", source_text="test")
        ],
    )
    service = MagicMock()
    service.runtime_keys.return_value = []

    with pytest.raises(SubtitleTranslateError, match="Chưa cấu hình Gemini API key"):
        translate_source_document(service, doc)
