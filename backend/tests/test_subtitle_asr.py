from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.services.subtitle_asr import (
    SubtitleAsrCanceled,
    SubtitleAsrError,
    asr_cache_key,
    extract_subtitles_asr,
)


def test_extract_subtitles_asr_no_audio(tmp_path):
    video_file = tmp_path / "silent.mp4"
    video_file.write_bytes(b"data")

    media = {"fingerprint": "mock_silent", "has_audio": False, "duration_ms": 5000}

    with pytest.raises(SubtitleAsrError, match="Video không có âm thanh"):
        extract_subtitles_asr(video_file, media)


def test_extract_subtitles_asr_model_missing(tmp_path):
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"data")

    media = {"fingerprint": "mock_fp", "has_audio": True, "duration_ms": 5000}

    from huggingface_hub.errors import LocalEntryNotFoundError

    with (
        patch("app.services.subtitle_asr._extract_audio_pcm", return_value=np.zeros(16000, dtype=np.float32)),
        patch("faster_whisper.utils.download_model", side_effect=LocalEntryNotFoundError("Model not found locally")),
        pytest.raises(SubtitleAsrError, match="chưa được cài đặt cục bộ"),
    ):
        extract_subtitles_asr(
            video_file,
            media,
            model_name="large-v3",
            allow_download=False,
            model_dir=tmp_path / "empty_cache",
        )


def test_extract_subtitles_asr_mock_transcribe(tmp_path):
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"data")

    media = {
        "fingerprint": "audio_test_fp",
        "audio_hash": "a1b2c3d4e5",
        "has_audio": True,
        "duration_ms": 10000,
    }

    # Simulate faster-whisper transcribe output with VAD silence skipping
    # Segment 1: start at 1.5s, end at 3.2s
    # (Notice: silence from 0s to 1.5s was skipped by VAD, but timestamp 1.5s is on video timeline!)
    class MockWord:
        def __init__(self, word, start, end, prob):
            self.word = word
            self.start = start
            self.end = end
            self.probability = prob

    class MockSegment:
        def __init__(self, text, start, end, words):
            self.text = text
            self.start = start
            self.end = end
            self.words = words

    class MockInfo:
        language = "zh"
        language_probability = 0.98

    seg1 = MockSegment(
        "你好 世界",
        1.5,
        3.2,
        [MockWord("你好", 1.5, 2.2, 0.95), MockWord("世界", 2.3, 3.2, 0.96)],
    )
    seg2 = MockSegment(
        "明天 见",
        6.0,
        7.5,
        [MockWord("明天", 6.0, 6.7, 0.92), MockWord("见", 6.8, 7.5, 0.94)],
    )

    mock_whisper = MagicMock()
    mock_whisper.transcribe.return_value = ([seg1, seg2], MockInfo())

    with patch("app.services.subtitle_asr._extract_audio_pcm", return_value=np.zeros(16000 * 10, dtype=np.float32)), \
         patch("app.services.subtitle_asr._resolve_whisper_model", return_value="dummy_path"), \
         patch("app.services.subtitle_asr._load_cached_whisper_model", return_value=mock_whisper):

        result = extract_subtitles_asr(
            video_file,
            media,
            source_language=None,
            model_name="small",
            device="cpu",
        )

        doc = result["document"]
        assert doc["timing_source"] == "asr"
        assert result["detected_language"] == "zh"

        segments = doc["segments"]
        assert len(segments) == 2

        # Segment 1 preserves timeline offset 1500ms despite silence before it
        assert segments[0]["start_ms"] == 1500
        assert segments[0]["end_ms"] == 3200
        assert segments[0]["source_text"] == "你好 世界"
        assert segments[0]["content_source"] == "audio"
        assert len(segments[0]["words"]) == 2

        # Segment 2 starts at 6000ms
        assert segments[1]["start_ms"] == 6000
        assert segments[1]["end_ms"] == 7500
        assert segments[1]["source_text"] == "明天 见"


def test_extract_subtitles_asr_cancellation(tmp_path):
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"data")

    context = MagicMock()
    context.raise_if_canceled.side_effect = SubtitleAsrCanceled("ASR Canceled")

    with (
        patch("app.services.subtitle_asr._extract_audio_pcm", return_value=np.zeros(16000, dtype=np.float32)),
        pytest.raises(SubtitleAsrCanceled),
    ):
        extract_subtitles_asr(
            video_file,
            {"has_audio": True, "duration_ms": 2000},
            context=context,
        )


def test_extract_subtitles_asr_resumes_completed_windows(tmp_path):
    video_file = tmp_path / "long-video.mp4"
    video_file.write_bytes(b"data")
    media = {
        "fingerprint": "long-audio-fp",
        "audio_hash": "long-audio-hash",
        "has_audio": True,
        "duration_ms": 120000,
    }

    class MockSegment:
        def __init__(self, text, start, end):
            self.text = text
            self.start = start
            self.end = end
            self.words = []

    class MockInfo:
        language = "vi"
        language_probability = 0.91

    first_window = [MockSegment("câu đầu", 1.0, 2.0)]
    second_window = [MockSegment("câu sau", 2.0, 3.0)]
    cache_dir = tmp_path / "asr-cache"
    audio = np.zeros(16000, dtype=np.float32)

    interrupted_model = MagicMock()
    interrupted_model.transcribe.side_effect = [
        (first_window, MockInfo()),
        RuntimeError("worker interrupted"),
    ]
    with (
        patch("app.services.subtitle_asr._extract_audio_pcm", side_effect=[audio, audio]),
        patch("app.services.subtitle_asr._resolve_whisper_model", return_value="dummy_path"),
        patch(
            "app.services.subtitle_asr._load_cached_whisper_model",
            return_value=interrupted_model,
        ),
        pytest.raises(SubtitleAsrError, match="worker interrupted"),
    ):
        extract_subtitles_asr(
            video_file,
            media,
            model_name="small",
            device="cpu",
            cache_dir=cache_dir,
        )

    resumed_model = MagicMock()
    resumed_model.transcribe.return_value = (second_window, MockInfo())
    with (
        patch("app.services.subtitle_asr._extract_audio_pcm", return_value=audio) as extract_audio,
        patch("app.services.subtitle_asr._resolve_whisper_model", return_value="dummy_path"),
        patch(
            "app.services.subtitle_asr._load_cached_whisper_model",
            return_value=resumed_model,
        ),
    ):
        result = extract_subtitles_asr(
            video_file,
            media,
            model_name="small",
            device="cpu",
            cache_dir=cache_dir,
        )

    assert result["metrics"]["checkpoint_windows"] == 1
    assert result["metrics"]["windows"] == 1
    assert extract_audio.call_count == 1
    assert [cue["text"] for cue in result["document"]["segments"]] == [
        "câu đầu",
        "câu sau",
    ]
    assert [cue["start_ms"] for cue in result["document"]["segments"]] == [1000, 60000]


def test_asr_cache_key_sensitivity():
    m1 = {"audio_hash": "hash1", "duration_ms": 5000}
    m2 = {"audio_hash": "hash2", "duration_ms": 5000}

    k1 = asr_cache_key(m1, source_language="en", model_name="small", device="cpu", compute_type="int8")
    k2 = asr_cache_key(m2, source_language="en", model_name="small", device="cpu", compute_type="int8")
    k3 = asr_cache_key(m1, source_language="zh", model_name="small", device="cpu", compute_type="int8")
    k4 = asr_cache_key(m1, source_language="en", model_name="base", device="cpu", compute_type="int8")

    assert k1 != k2
    assert k1 != k3
    assert k1 != k4
