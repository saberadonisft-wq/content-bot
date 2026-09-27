"""Real lossless video decode + deterministic OCR oracle. No network or user media."""

from fractions import Fraction

import av
import numpy as np
import pytest

from app.schemas import SubtitleOcrRegion
from app.services import subtitle_ocr as ocr

LABELS = {
    40: "I have 100 apples",
    80: "I have 200 apples",
    120: "I do not agree",
    160: "I do agree",
    200: "Nguyễn An",
    240: "Nguyễn Anh",
}


def write_video(path, times, values, rotation=0, offset=0):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=20)
        stream.width, stream.height = (120, 200) if rotation % 180 else (200, 120)
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)
        for timestamp, value in zip(times, values):
            display = np.zeros((120, 200, 3), np.uint8)
            display[84:108, 20:180] = value
            raw = np.ascontiguousarray(np.rot90(display, -(rotation // 90)))
            frame = av.VideoFrame.from_ndarray(raw, format="bgr24")
            frame.pts, frame.time_base = timestamp + offset, Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def run_video(tmp_path, monkeypatch, times, values, *, rotation=0, offset=0, **options):
    path = tmp_path / "synthetic.mkv"
    write_video(path, times, values, rotation, offset)
    monkeypatch.setattr(ocr, "_get_ocr_engine", lambda: object())

    def read(engine, crop):
        value = int(np.median(crop))
        return LABELS.get(value, ""), 0.98 if value in LABELS else 0.0, ()

    monkeypatch.setattr(ocr, "_extract_crop_text", read)
    return ocr.extract_subtitles_ocr(
        path,
        SubtitleOcrRegion(x=10, y=70, width=80, height=20),
        {
            "duration_ms": times[-1] + 50,
            "fingerprint": path.name,
            "source_start_ms": offset,
            "rotation": rotation,
        },
        sample_fps=2,
        min_duration_ms=100,
        glyph_cache=False,
        **options,
    )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("offset", [0, 5000])
def test_rotated_offset_video_keeps_changed_numbers_and_frame_boundaries(
    tmp_path, monkeypatch, rotation, offset
):
    times = list(range(0, 1500, 50))
    values = [40 if t < 650 else 80 for t in times]
    result = run_video(
        tmp_path, monkeypatch, times, values, rotation=rotation, offset=offset
    )
    cues = result["document"]["segments"]
    assert [(c["text"], c["start_ms"], c["end_ms"]) for c in cues] == [
        (LABELS[40], 0, 650),
        (LABELS[80], 650, 1500),
    ]
    assert result["metrics"]["peak_refinement_bytes"] <= ocr.REFINEMENT_MAX_BYTES
    assert result["metrics"]["ocr_calls"] < len(times) // 2
    metrics = result["metrics"]
    assert metrics["ocr_calls"] == (
        metrics["sample_ocr_calls"] + metrics["refinement_calls"]
    )
    assert metrics["ocr_observations"] == (
        metrics["sample_observations"] + metrics["refinement_observations"]
    )
    assert metrics["refinement_observations"] >= metrics["refinement_calls"]
    assert all(value >= 0 for value in result["timings_seconds"].values())


def test_vfr_negation_names_and_repeated_subtitles_are_separate(tmp_path, monkeypatch):
    times = [
        0,
        80,
        220,
        450,
        600,
        760,
        900,
        1010,
        1200,
        1460,
        1600,
        1800,
        2010,
        2200,
        2450,
        2600,
        2800,
    ]
    values = [120] * 4 + [160] * 4 + [0] * 2 + [160] * 2 + [200] * 3 + [240] * 2
    result = run_video(tmp_path, monkeypatch, times, values)
    cues = result["document"]["segments"]
    assert [c["text"] for c in cues] == [
        LABELS[120],
        LABELS[160],
        LABELS[160],
        LABELS[200],
        LABELS[240],
    ]
    assert [c["start_ms"] for c in cues] == [0, 600, 1600, 2010, 2600]
    assert cues[1]["end_ms"] == 1200


def test_short_intermediate_and_final_display_are_not_lost(tmp_path, monkeypatch):
    times = list(range(0, 1300, 50))
    values = [80 if 150 <= t < 350 or t >= 1100 else 40 for t in times]
    cues = run_video(tmp_path, monkeypatch, times, values)["document"]["segments"]
    assert [(c["text"], c["start_ms"], c["end_ms"]) for c in cues] == [
        (LABELS[40], 0, 150),
        (LABELS[80], 150, 350),
        (LABELS[40], 350, 1100),
        (LABELS[80], 1100, 1300),
    ]


def test_inference_failure_does_not_become_a_blank_observation():
    def broken(image):
        raise RuntimeError("fixture inference failed")

    with pytest.raises(ocr.SubtitleOcrError, match="fixture inference failed"):
        ocr._extract_crop_text(broken, np.zeros((40, 100, 3), np.uint8))


def test_decoder_cancellation_is_not_wrapped_as_failure(tmp_path, monkeypatch):
    from app.services.subtitle_jobs import SubtitleJobCanceled

    path = tmp_path / "cancel.mkv"
    write_video(path, list(range(0, 1000, 50)), [40] * 20)
    monkeypatch.setattr(ocr, "_get_ocr_engine", lambda: object())

    class Context:
        calls = 0

        def update(self, *a):
            pass

        def raise_if_canceled(self):
            self.calls += 1
            if self.calls > 5:
                raise SubtitleJobCanceled("fixture canceled during decode")

    monkeypatch.setattr(ocr, "_extract_crop_text", lambda *a: ("text", 0.9, ()))
    with pytest.raises(SubtitleJobCanceled, match="during decode"):
        ocr.extract_subtitles_ocr(
            path, SubtitleOcrRegion(), {"duration_ms": 1000}, context=Context(), glyph_cache=False,
        )


def test_warm_cache_skips_probe_and_decode_and_rejects_corrupt_json(
    tmp_path, monkeypatch
):
    path = tmp_path / "cached.mkv"
    write_video(path, list(range(0, 1000, 50)), [40] * 20)
    region = SubtitleOcrRegion(x=10, y=70, width=80, height=20)
    calls = []
    monkeypatch.setattr(ocr, "_get_ocr_engine", lambda: object())
    monkeypatch.setattr(ocr, "_extract_crop_text", lambda *a: ("Hello", 0.95, ()))
    monkeypatch.setattr(
        ocr,
        "probe_subtitle_y_band",
        lambda *a, **kw: (calls.append("probe") or region, "en"),
    )
    options = {"auto_probe": True, "cache_dir": tmp_path / "cache", "glyph_cache": False}
    first = ocr.extract_subtitles_ocr(path, region, {"duration_ms": 1000}, **options)
    second = ocr.extract_subtitles_ocr(path, region, {"duration_ms": 1000}, **options)
    assert calls == ["probe"]
    assert second["cache_hit"] is True
    assert second["metrics"]["ocr_calls"] == second["metrics"]["decoded_frames"] == 0
    assert second["metrics"]["ocr_observations"] == 0
    assert second["cached_timings_seconds"] is not None
    assert second["timings_seconds"]["decode_seconds"] == 0
    assert second["timings_seconds"]["cache_lookup_seconds"] >= 0
    assert first["document"] == second["document"]
    cache = options["cache_dir"] / f"ocr_{first['cache_key']}.json"
    cache.write_text("{broken", encoding="utf-8")
    recovered = ocr.extract_subtitles_ocr(
        path, region, {"duration_ms": 1000}, **options
    )
    assert recovered["document"] == first["document"]
    assert calls == ["probe", "probe"]


def test_repeated_text_with_short_confirmed_blank_is_separate(tmp_path, monkeypatch):
    times = list(range(0, 1500, 50))
    values = [0 if 650 <= t < 750 else 40 for t in times]
    cues = run_video(tmp_path, monkeypatch, times, values, max_gap_ms=1000)["document"][
        "segments"
    ]
    assert [(c["start_ms"], c["end_ms"]) for c in cues] == [(0, 650), (750, 1500)]
