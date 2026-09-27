import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.schemas import SubtitleOcrRegion
from app.services.subtitle_ocr import (
    SubtitleOcrCanceled,
    _empty_ocr_timings,
    _extract_crop_text,
    _timed_extract_crop_text,
    extract_subtitles_ocr,
    ocr_cache_key,
    text_similarity,
)


def test_levenshtein_and_text_similarity():
    # Exact match
    assert text_similarity("你好世界", "你好世界") == 1.0
    assert text_similarity("Hello World", "hello world") == 1.0

    # Minor jitter (1 char different in a 6 char string)
    assert text_similarity("Chào các bạn", "Chao cac ban") >= 0.8
    # Completely different
    assert text_similarity("你好", "Tạm biệt") <= 0.2


def test_extract_crop_text_multiline_ordering():
    # Mock engine that returns multi-line bounding boxes in reverse vertical order
    engine = MagicMock()
    engine.return_value = (
        [
            # Line 2: y around 60
            ([[10, 60], [100, 60], [100, 80], [10, 80]], "Dòng thứ hai", "0.95"),
            # Line 1: y around 20
            ([[10, 20], [100, 20], [100, 40], [10, 40]], "Dòng thứ nhất", "0.98"),
        ],
        0.05,
    )

    crop = np.zeros((100, 200, 3), dtype=np.uint8)
    text, conf, boxes = _extract_crop_text(engine, crop)
    assert len(boxes) == 2

    # Line 1 must come before Line 2
    assert text == "Dòng thứ nhất\nDòng thứ hai"
    assert round(conf, 2) == 0.96


def test_extract_crop_text_filter_low_confidence():
    engine = MagicMock()
    engine.return_value = (
        [
            ([[10, 10], [50, 10], [50, 20], [10, 20]], "artifact", "0.15"),
            ([[10, 30], [100, 30], [100, 50], [10, 50]], "Chữ rõ ràng", "0.92"),
        ],
        0.02,
    )
    crop = np.zeros((60, 120, 3), dtype=np.uint8)
    text, conf, boxes = _extract_crop_text(engine, crop)
    assert len(boxes) == 1
    assert text == "Chữ rõ ràng"
    assert round(conf, 2) == 0.92


def test_timed_extract_records_inference_without_timing_thresholds():
    import time

    class SlowBlankEngine:
        def __call__(self, _crop):
            time.sleep(0.01)
            return [], None

    timings = _empty_ocr_timings()
    text, confidence, boxes, cache_hits = _timed_extract_crop_text(
        SlowBlankEngine(),
        np.zeros((20, 40, 3), dtype=np.uint8),
        timings,
        "sample_inference_seconds",
    )

    assert (text, confidence) == ("", 0.0)
    assert boxes == () and cache_hits == 0
    assert timings["inference_lock_wait_seconds"] >= 0
    assert timings["sample_inference_seconds"] > 0
    assert timings["ocr_call_wall_seconds"] >= timings["sample_inference_seconds"]


def test_ocr_cache_key_sensitivity():
    media = {"fingerprint": "abc123456", "duration_ms": 10000}
    r1 = SubtitleOcrRegion(x=10, y=70, width=80, height=15)
    r2 = SubtitleOcrRegion(x=10, y=75, width=80, height=15)

    k1 = ocr_cache_key(media, r1, source_language="zh", sample_fps=5.0, min_duration_ms=300)
    k2 = ocr_cache_key(media, r2, source_language="zh", sample_fps=5.0, min_duration_ms=300)
    k3 = ocr_cache_key(media, r1, source_language="en", sample_fps=5.0, min_duration_ms=300)

    assert k1 != k2
    assert k1 != k3


def test_extract_subtitles_ocr_mock_stream(tmp_path):
    video_file = tmp_path / "mock.mp4"
    video_file.write_bytes(b"mock video data")

    region = SubtitleOcrRegion(x=10, y=70, width=80, height=15)
    media = {"fingerprint": "mock_fp", "duration_ms": 4000, "has_audio": False}

    # Simulate 8 frames over 4000ms:
    # Frame 0 (pts 0): "Xin chào"
    # Frame 1 (pts 500): "Xin chào" (same display)
    # Frame 2 (pts 1000): "Xin chao" (changed pixels and text: preserve until confirmed as noise)
    # Frame 3 (pts 1500): "" (gap start)
    # Frame 4 (pts 2000): "" (gap continue > 250ms -> closes first segment)
    # Frame 5 (pts 2500): "Xin chào" (same sentence reappearing after gap -> separate segment!)
    # Frame 6 (pts 3000): "Tạm biệt" (immediate text change -> closes previous segment!)
    # Frame 7 (pts 3500): "Tạm biệt"
    frames_meta = [
        (0, "Xin chào", 0.95),
        (500, "Xin chào", 0.95),
        (1000, "Xin chao", 0.90),
        (1500, "", 0.0),
        (2000, "", 0.0),
        (2500, "Xin chào", 0.94),
        (3000, "Tạm biệt", 0.96),
        (3500, "Tạm biệt", 0.97),
    ]

    class MockFrame:
        def __init__(self, pts_ms):
            self.pts = pts_ms
            self.time = pts_ms / 1000.0

        def to_ndarray(self, format="bgr24"):
            return np.full((100, 200, 3), fill_value=(self.pts // 10) % 250, dtype=np.uint8)

    class MockStream:
        time_base = 1 / 1000
        width = 200
        height = 100
        codec_context = MagicMock()

    class MockContainer:
        def __init__(self):
            self.streams = MagicMock()
            self.streams.video = [MockStream()]

        def decode(self, video=0):
            for pts_ms, _, _ in frames_meta:
                yield MockFrame(pts_ms)

        def close(self):
            pass

    call_index = [0]

    def mock_extract(engine, crop):
        idx = call_index[0]
        call_index[0] += 1
        _, text, conf = frames_meta[idx]
        return text, conf, ()

    with patch("av.open", return_value=MockContainer()), \
         patch("app.services.subtitle_ocr._extract_crop_text", side_effect=mock_extract), \
         patch("app.services.subtitle_ocr._get_ocr_engine", return_value=MagicMock()):

        result = extract_subtitles_ocr(
            video_file,
            region,
            media,
            sample_fps=2.0,
            min_duration_ms=200,
            max_gap_ms=250,
            glyph_cache=False,
        )

        doc = result["document"]
        segments = doc["segments"]

        # Accents can change meaning. Without evidence that a changed reading is noise,
        # retaining it is safer than silently folding it into the previous sentence.
        assert len(segments) == 4
        assert segments[1]["text"] == "Xin chao"

        # Segment 1
        assert segments[0]["start_ms"] == 0
        assert "Xin ch" in segments[0]["text"]
        assert segments[0]["timing_source"] == "ocr"

        # Segment 2 (reappeared same sentence after gap must be separate)
        assert segments[2]["start_ms"] >= 2000
        assert segments[2]["text"] == "Xin chào"

        # Segment 3 (different text)
        assert segments[3]["start_ms"] >= 2900
        assert segments[3]["text"] == "Tạm biệt"


def test_extract_subtitles_ocr_cancellation(tmp_path):
    video_file = tmp_path / "mock.mp4"
    video_file.write_bytes(b"data")

    cancel_event = threading.Event()
    cancel_event.set()

    context = MagicMock()
    context.cancel_event = cancel_event
    context.raise_if_canceled.side_effect = SubtitleOcrCanceled("Canceled")

    with pytest.raises(SubtitleOcrCanceled):
        extract_subtitles_ocr(
            video_file,
            SubtitleOcrRegion(),
            {"fingerprint": "f", "duration_ms": 5000},
            context=context,
        )


def test_extract_subtitles_ocr_vfr_timestamps(tmp_path):
    video_file = tmp_path / "vfr.mp4"
    video_file.write_bytes(b"data")

    # Irregular VFR frame timestamps (pts in ms)
    vfr_frames = [
        (0, "Câu mở đầu", 0.95),
        (133, "Câu mở đầu", 0.95),
        (367, "Câu mở đầu", 0.95),
        (800, "", 0.0),
        (1250, "Câu kết thúc", 0.92),
        (1600, "Câu kết thúc", 0.92),
    ]

    class MockFrame:
        def __init__(self, pts_ms):
            self.pts = pts_ms
            self.time = pts_ms / 1000.0

        def to_ndarray(self, format="bgr24"):
            return np.full((100, 200, 3), fill_value=(self.pts // 10) % 250, dtype=np.uint8)

    class MockStream:
        time_base = 1 / 1000
        width = 200
        height = 100
        codec_context = MagicMock()

    class MockContainer:
        def __init__(self):
            self.streams = MagicMock()
            self.streams.video = [MockStream()]

        def decode(self, video=0):
            for pts_ms, _, _ in vfr_frames:
                yield MockFrame(pts_ms)

        def close(self):
            pass

    call_index = [0]

    def mock_extract(engine, crop):
        idx = call_index[0]
        call_index[0] += 1
        _, text, conf = vfr_frames[idx]
        return text, conf, ()

    with patch("av.open", return_value=MockContainer()), \
         patch("app.services.subtitle_ocr._extract_crop_text", side_effect=mock_extract), \
         patch("app.services.subtitle_ocr._get_ocr_engine", return_value=MagicMock()):

        result = extract_subtitles_ocr(
            video_file,
            SubtitleOcrRegion(),
            {"fingerprint": "vfr_test", "duration_ms": 2000},
            sample_fps=10.0,
            min_duration_ms=100,
            max_gap_ms=200,
            glyph_cache=False,
        )

        segs = result["document"]["segments"]
        assert len(segs) == 2
        # Seg 1 started at pts 0
        assert segs[0]["start_ms"] == 0
        assert segs[0]["text"] == "Câu mở đầu"
        # Seg 2 started at actual VFR pts 1250, not uniform grid!
        assert segs[1]["start_ms"] >= 1200
        assert segs[1]["text"] == "Câu kết thúc"


def test_real_rapidocr_on_synthesized_image():
    from PIL import Image, ImageDraw

    from app.services.subtitle_ocr import _extract_crop_text, _get_ocr_engine

    # Draw clear text on white canvas
    img = Image.new("RGB", (240, 60), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((10, 15), "HELLO 2026", fill=(0, 0, 0))

    img_bgr = np.array(img)[:, :, ::-1]
    engine = _get_ocr_engine()
    text, conf, _ = _extract_crop_text(engine, img_bgr)

    assert "HELLO" in text.upper()
    assert conf > 0.5


def test_detect_dominant_script():
    from app.services.subtitle_ocr import detect_dominant_script

    assert detect_dominant_script("你好世界！这是中文") == "zh"
    assert detect_dominant_script("안녕하세요 여러분") == "ko"
    assert detect_dominant_script("こんにちは世界") == "ja"
    assert detect_dominant_script("Xin chào mọi người đây là tiếng Việt") == "latin"
    assert detect_dominant_script("Hello world this is English") == "latin"


def test_probe_subtitle_y_band_does_not_claim_detection_without_evidence(tmp_path):
    from app.services.subtitle_ocr import probe_subtitle_y_band

    fake_video = tmp_path / "empty.mp4"
    fake_video.write_bytes(b"empty")

    region, script = probe_subtitle_y_band(fake_video, {"duration_ms": 0})
    assert region is None
    assert script == "und"
