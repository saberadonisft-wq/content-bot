from __future__ import annotations

import time
from pathlib import Path

from app import main
from app.schemas import SubtitleJobResponse, SubtitleRenderRequestV2
from app.services.subtitle_jobs import SubtitleJobManager


def test_render_endpoint_submits_progress_and_attachable_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"fixture")
    overlay_path = tmp_path / "logo.png"
    overlay_path.write_bytes(b"fixture-overlay")
    manager = SubtitleJobManager(tmp_path / "jobs", max_workers=1)
    media = {
        "fingerprint": "1" * 64,
        "audio_hash": None,
        "duration_ms": 5000,
        "has_audio": False,
    }
    monkeypatch.setattr(main, "subtitle_jobs", manager)
    monkeypatch.setattr(main, "_uploaded_video_path", lambda _video_id: source)
    monkeypatch.setattr(main, "probe_media_cached", lambda *_args, **_kwargs: media)
    monkeypatch.setattr(
        main,
        "resolve_subtitle_overlay",
        lambda _data_dir, _overlay_id: overlay_path,
    )
    received: dict = {}

    def fake_render(*_args, **kwargs):
        received.update(kwargs)
        kwargs["progress"](65, "encoding", "Đang mã hóa video")
        return {
            "video_id": "a" * 12,
            "output_filename": "subtitled_fixture.mp4",
            "subtitled_video_url": "/api/v1/subtitles/renders/subtitled_fixture.mp4",
            "cache_hit": False,
            "duration_ms": 5000,
        }

    monkeypatch.setattr(main, "render_precision_video", fake_render)
    request = SubtitleRenderRequestV2(
        video_id="a" * 12,
        document={
            "schema_version": 2,
            "language": "vi",
            "timebase": "milliseconds",
            "timing_source": "manual",
            "timing_precision_ms": 1,
            "segments": [
                {
                    "id": "cue-1",
                    "start_ms": 1,
                    "end_ms": 4999,
                    "text": "Kiểm thử render job",
                    "timing_source": "manual",
                    "timing_precision_ms": 1,
                    "needs_review": False,
                    "revision": 0,
                }
            ],
        },
        options={"encoder": "software"},
        overlay={
            "overlay_id": "b" * 64,
            "x": 14,
            "y": 12,
            "width": 22,
        },
    )

    submitted = SubtitleJobResponse(
        **main.render_subtitle_timeline_v2_endpoint(request)
    )
    assert submitted.kind == "render"

    deadline = time.monotonic() + 3
    completed = None
    while time.monotonic() < deadline:
        completed = manager.get(submitted.id)
        if completed and completed["state"] == "succeeded":
            break
        time.sleep(0.01)

    assert completed is not None
    attached = SubtitleJobResponse(**main.get_subtitle_job(submitted.id))
    assert attached.state == "succeeded"
    assert attached.result is not None
    assert attached.result["duration_ms"] == 5000
    assert received["overlay_path"] == overlay_path
    assert received["overlay"] == {
        "overlay_id": "b" * 64,
        "x": 14.0,
        "y": 12.0,
        "width": 22.0,
    }
    assert (tmp_path / "jobs" / f"{submitted.id}.json").is_file()
    manager.shutdown()
