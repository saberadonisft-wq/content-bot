"""Lossless decoded media with controlled OCR jitter and actual glyph changes."""

from fractions import Fraction

import av
import cv2
import numpy as np
import pytest

from app.schemas import SubtitleOcrRegion
from app.services import subtitle_ocr as ocr
from app.services.subtitle_ocr_continuity import CaptionContinuity
from app.services.subtitle_ocr_tracking import OcrAcceleration

TEXT = "这次给大家介绍一种稍微特别一些的收藏"
BOXES = ((12, 12, 310, 65),)


def caption_frame(index, *, dot=False, blank=False):
    image = np.full((80, 330, 3), 40 + index % 30, np.uint8)
    if not blank:
        cv2.putText(image, "CAPTION EXAMPLE", (18, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 5)
        cv2.putText(image, "CAPTION EXAMPLE", (18, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        if dot:
            image[17:27, 195:205] = 0
            image[20:24, 198:202] = 255
    # Independent changing background/marker; never part of the caption box.
    image[0, 0, 0] = index
    return image


def extract(tmp_path, monkeypatch, labels, *, changed=(), blank=(), accelerated=True):
    path = tmp_path / "jitter.mkv"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=20)
        stream.width, stream.height, stream.pix_fmt = 330, 80, "bgr0"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 1000)
        for index in range(len(labels)):
            image = caption_frame(index, dot=index in changed, blank=index in blank)
            frame = av.VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts, frame.time_base = index * 50, Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    class Engine:
        def __call__(self, image):
            index = int(image[0, 0, 0])
            if index in blank:
                return [], None
            x1, y1, x2, y2 = BOXES[0]
            box = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            return [[box, labels[index], 0.85 if labels[index] == TEXT else 0.99]], None

    monkeypatch.setattr(ocr, "_get_ocr_engine", Engine)
    return ocr.extract_subtitles_ocr(
        path, SubtitleOcrRegion(x=0, y=0, width=100, height=100),
        {"duration_ms": len(labels) * 50}, sample_fps=2, min_duration_ms=100,
        acceleration=OcrAcceleration(
            selective_refinement=accelerated, recognition_reuse=accelerated,
            refinement_batch_size=4 if accelerated else 1, crop_before_bgr=False,
        ),
    )


@pytest.mark.parametrize("accelerated", [False, True])
def test_long_caption_and_two_short_wrong_readings_form_one_cue(tmp_path, monkeypatch, accelerated):
    labels = [TEXT] * 30 + [TEXT.replace("大", "天")] * 7 + [TEXT] * 2 + [TEXT.replace("大", "犬")] * 7
    result = extract(tmp_path, monkeypatch, labels, accelerated=accelerated)
    cues = result["document"]["segments"]
    assert [(c["text"], c["start_ms"], c["end_ms"]) for c in cues] == [(TEXT, 0, 2300)]
    assert cues[0]["needs_review"] is True
    assert result["metrics"]["stabilized_observations"] > 0


def test_early_misreading_does_not_win_over_longer_correct_caption(tmp_path, monkeypatch):
    labels = [TEXT.replace("大", "天")] * 3 + [TEXT] * 37
    cues = extract(tmp_path, monkeypatch, labels)["document"]["segments"]
    assert [(c["text"], c["start_ms"], c["end_ms"]) for c in cues] == [(TEXT, 0, 2000)]


@pytest.mark.parametrize("accelerated", [False, True])
def test_actual_small_glyph_change_survives_similar_text(tmp_path, monkeypatch, accelerated):
    labels = [TEXT] * 20 + [TEXT.replace("大", "犬")] * 10 + [TEXT] * 10
    cues = extract(tmp_path, monkeypatch, labels, changed=range(20, 30), accelerated=accelerated)["document"]["segments"]
    assert [(c["text"], c["start_ms"], c["end_ms"]) for c in cues] == [
        (TEXT, 0, 1000), (TEXT.replace("大", "犬"), 1000, 1500), (TEXT, 1500, 2000),
    ]


def test_blank_resets_consensus_even_when_caption_returns(tmp_path, monkeypatch):
    labels = [TEXT] * 40
    cues = extract(tmp_path, monkeypatch, labels, blank=range(24, 26))["document"]["segments"]
    assert [(c["start_ms"], c["end_ms"]) for c in cues] == [(0, 1200), (1300, 2000)]


@pytest.mark.parametrize("left,right", [
    ("I have 100 apples", "I have 200 apples"),
    ("I do not agree", "I do agree"),
    ("Nguyễn An", "Nguyễn Anh"),
    ("Xin chào tất cả các bạn", "Xin chao tất cả các bạn"),
    (TEXT, TEXT.replace("大", "不")),
    (TEXT, TEXT.replace("一", "二", 1)),
])
def test_critical_text_changes_are_not_smoothed_even_with_matching_pixels(left, right):
    image = caption_frame(0)
    anchor = CaptionContinuity.create(left, image, BOXES)
    assert anchor is not None
    assert not anchor.matches(right, image, BOXES)


def test_spaces_can_jitter_but_missing_geometry_cannot_establish_continuity():
    image = caption_frame(0)
    anchor = CaptionContinuity.create(TEXT, image, BOXES)
    assert anchor.matches(TEXT.replace("大", " 大 "), image, BOXES)
    assert not anchor.matches(TEXT.replace("大", "天"), image, ())
    assert CaptionContinuity.create(TEXT, image, ()) is None
    assert CaptionContinuity.create(TEXT, np.full_like(image, 255), BOXES) is None
