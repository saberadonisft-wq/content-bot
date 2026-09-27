"""Small media-backed Studio pipeline regression.

The OCR engine is deterministic here so the test exercises real PyAV decode,
job persistence, version storage, FFmpeg subtitle rendering and output probing
without requiring a paid API or a downloaded OCR model.
"""

from __future__ import annotations

import time
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np

from app.api import subtitles as subtitles_api
from app.schemas import SubtitleOcrRequest, SubtitleRenderRequestV2
from app.services import subtitle_ocr as ocr
from app.services.media_probe import probe_media
from app.services.subtitle_jobs import SubtitleJobManager


def _write_fixture(path):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=12)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "bgr0"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 1000)
        for timestamp in range(0, 2000, 100):
            value = 40 if timestamp < 1000 else 80
            image = np.zeros((180, 320, 3), dtype=np.uint8)
            image[128:160, 32:288] = value
            frame = av.VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts, frame.time_base = timestamp, Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _wait(manager, job_id):
    deadline = time.monotonic() + 30
    record = manager.get(job_id)
    while record and record["state"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.02)
        record = manager.get(job_id)
    assert record is not None
    assert record["state"] == "succeeded", record.get("error") or record
    return record


def test_real_media_ocr_version_and_render_pipeline(tmp_path, monkeypatch):
    video_id = "d" * 16
    source = tmp_path / "source.mkv"
    _write_fixture(source)
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda _video_id: source)
    monkeypatch.setattr(subtitles_api.settings, "content_bot_data_dir", tmp_path)
    monkeypatch.setattr(
        subtitles_api.settings, "content_bot_subtitle_ocr_device_policy", "cpu"
    )
    labels = {40: "I have 100 apples", 80: "I have 200 apples"}

    def engine(crop):
        value = int(np.median(crop))
        height, width = crop.shape[:2]
        box = [[0, 0], [width, 0], [width, height], [0, height]]
        return ([[box, labels[value], 0.99]] if value in labels else []), None

    monkeypatch.setattr(ocr, "_get_ocr_engine", lambda: engine)
    jobs = SubtitleJobManager(tmp_path / "jobs")
    services = SimpleNamespace(subtitle_jobs=jobs)
    try:
        extraction = subtitles_api.extract_subtitles_ocr_endpoint(
            SubtitleOcrRequest(
                video_id=video_id,
                region={"x": 10, "y": 70, "width": 80, "height": 20},
                source_language="en",
                sample_fps=5,
                min_duration_ms=100,
            ),
            services=services,
        )
        extracted = _wait(jobs, extraction["id"])
        document = extracted["result"]["document"]
        assert [cue["text"] for cue in document["segments"]] == [
            "I have 100 apples",
            "I have 200 apples",
        ]
        assert extracted["result"]["version_id"]

        rendered = subtitles_api.render_subtitle_timeline_v2_endpoint(
            SubtitleRenderRequestV2(video_id=video_id, document=document),
            user={"sub": "media-e2e"},
            services=services,
        )
        output_job = _wait(jobs, rendered["id"])
        output = tmp_path / "videos" / "output" / output_job["result"]["output_filename"]
        assert output.is_file()
        output_media = probe_media(output)
        assert output_media["duration_ms"] >= 1900
        assert output_media["width"] == 320
        assert output_media["height"] == 180
    finally:
        jobs.shutdown(wait=True)
