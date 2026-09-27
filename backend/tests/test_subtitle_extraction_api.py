from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_export_source_srt_endpoint(client):
    doc = {
        "schema_version": 2,
        "language": "zh",
        "timebase": "milliseconds",
        "timing_source": "ocr",
        "timing_precision_ms": 100,
        "segments": [
            {
                "id": "ocr_0001",
                "start_ms": 1000,
                "end_ms": 2500,
                "text": "Bản dịch tiếng Việt",
                "source_text": "你好世界",
                "source_language": "zh",
                "content_source": "screen",
                "timing_source": "ocr",
                "timing_precision_ms": 100,
                "needs_review": False,
                "revision": 0,
            }
        ],
    }

    resp = client.post("/api/v1/subtitles/v2/export/source-srt", json={"document": doc})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    # Must contain the source text "你好世界", not the translated text!
    assert "你好世界" in data["srt"]
    assert "00:00:01,000 --> 00:00:02,500" in data["srt"]


def test_extract_ocr_endpoint(client, tmp_path):
    media = {
        "fingerprint": "a1b2c3d4e5f67890",
        "file_size_bytes": 1000,
        "duration_ms": 5000,
        "source_start_ms": 0,
        "time_base_numerator": 1,
        "time_base_denominator": 30,
        "frame_rate_numerator": 30,
        "frame_rate_denominator": 1,
        "average_fps": 30.0,
        "frame_count": 150,
        "is_vfr": False,
        "frame_pts_ms": [],
        "width": 1280,
        "height": 720,
        "rotation": 0,
        "video_codec": "h264",
        "has_audio": True,
        "audio_codec": "aac",
    }

    mock_doc = {
        "schema_version": 2,
        "language": "zh",
        "timebase": "milliseconds",
        "timing_source": "ocr",
        "timing_precision_ms": 100,
        "segments": [],
    }

    with patch("app.api.subtitles._uploaded_video_path", return_value=tmp_path / "mock.mp4"), \
         patch("app.api.subtitles.probe_media_cached", return_value=media), \
         patch("app.api.subtitles.extract_subtitles_ocr", return_value={"document": mock_doc, "segment_count": 0}):

        resp = client.post(
            "/api/v1/subtitles/v2/extract/ocr",
            json={
                "video_id": "a1b2c3d4e5f67890",
                "region": {"x": 15, "y": 70, "width": 70, "height": 20},
                "source_language": "zh",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "ocr"
        assert data["state"] in {"queued", "running", "succeeded"}


def test_extract_asr_endpoint_no_audio(client, tmp_path):
    media = {
        "fingerprint": "a1b2c3d4e5f67890",
        "has_audio": False,
        "duration_ms": 5000,
    }

    with patch("app.api.subtitles._uploaded_video_path", return_value=tmp_path / "mock.mp4"), \
         patch("app.api.subtitles.probe_media_cached", return_value=media):

        resp = client.post(
            "/api/v1/subtitles/v2/extract/asr",
            json={
                "video_id": "a1b2c3d4e5f67890",
                "source_language": "en",
            },
        )
        assert resp.status_code == 422
        assert "Video không có âm thanh" in resp.json()["detail"]


def test_translate_gemini_endpoint(client, tmp_path):
    doc = {
        "schema_version": 2,
        "language": "zh",
        "timebase": "milliseconds",
        "timing_source": "ocr",
        "timing_precision_ms": 100,
        "segments": [
            {
                "id": "c001",
                "start_ms": 500,
                "end_ms": 1500,
                "text": "Hello",
                "source_text": "Hello",
                "source_language": "en",
                "content_source": "audio",
                "timing_source": "asr",
                "timing_precision_ms": 10,
                "needs_review": False,
                "revision": 0,
            }
        ],
    }

    translated_doc = {
        **doc,
        "language": "vi",
        "segments": [
            {
                **doc["segments"][0],
                "text": "Xin chào",
                "secondary_text": "Hello",
            }
        ],
    }

    with patch("app.api.subtitles.translate_source_document", return_value={"document": translated_doc, "translated_count": 1}):
        resp = client.post(
            "/api/v1/subtitles/v2/translate/gemini",
            json={
                "video_id": "a1b2c3d4e5f67890",
                "document": doc,
                "target_language": "vi",
                "bilingual": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "translation"
