from __future__ import annotations

import time
from pathlib import Path

from app.api import subtitles as subtitles_api
from app.schemas import (
    SubtitleAssPreviewRequestV2,
    SubtitleJobResponse,
    SubtitleRenderRequestV2,
)
from app.services.subtitle_jobs import SubtitleJobManager


def test_preview_ass_uses_percentage_anchor_and_output_aspect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"fixture")
    media = {
        "width": 1920,
        "height": 1080,
        "duration_ms": 5000,
        "has_audio": False,
    }
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda _video_id: source)
    monkeypatch.setattr(subtitles_api, "probe_media_cached", lambda *_args, **_kwargs: media)
    request = SubtitleAssPreviewRequestV2(
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
                    "start_ms": 100,
                    "end_ms": 900,
                    "text": "Phụ đề tỷ lệ",
                    "timing_source": "manual",
                    "timing_precision_ms": 1,
                    "needs_review": False,
                    "revision": 0,
                }
            ],
        },
        options={"font_size": 14, "pos_x": 25, "pos_y": 75},
    )

    response = subtitles_api.preview_subtitle_timeline_v2_endpoint(request)

    assert response.play_res_x == 1280
    assert response.play_res_y == 720
    assert "PlayResX: 1280" in response.ass
    assert "PlayResY: 720" in response.ass
    assert "Style: Default,Arimo,21.0" in response.ass
    assert r"\pos(320,540)" in response.ass


def test_render_request_allows_overlay_without_subtitle_cues() -> None:
    request = SubtitleRenderRequestV2(
        video_id="a" * 12,
        document={
            "schema_version": 2,
            "language": "vi",
            "timebase": "milliseconds",
            "timing_source": "manual",
            "timing_precision_ms": 1,
            "segments": [],
        },
        overlay={
            "overlay_id": "b" * 64,
            "x": 14,
            "y": 12,
            "width": 22,
        },
    )

    assert request.document.segments == []
    assert request.overlay is not None


def test_render_endpoint_submits_progress_and_attachable_result(
    application_services,
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
    monkeypatch.setattr(application_services, "subtitle_jobs", manager)
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda _video_id: source)
    monkeypatch.setattr(subtitles_api, "probe_media_cached", lambda *_args, **_kwargs: media)
    monkeypatch.setattr(
        subtitles_api,
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

    monkeypatch.setattr(subtitles_api, "render_precision_video", fake_render)
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
        masks=[
            {
                "id": "mask-1",
                "shape": "rounded",
                "effect": "blur",
                "x": 20,
                "y": 70,
                "width": 60,
                "height": 12,
                "strength": 14,
                "opacity": 0.85,
                "feather": 2,
                "cornerRadius": 18,
                "color": "#000000",
            }
        ],
    )

    submitted = SubtitleJobResponse(
        **subtitles_api.render_subtitle_timeline_v2_endpoint(
            request, services=application_services
        )
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
    attached = SubtitleJobResponse(
        **subtitles_api.get_subtitle_job(submitted.id, services=application_services)
    )
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
    assert received["masks"] == [
        {
            "id": "mask-1",
            "shape": "rounded",
            "effect": "blur",
            "x": 20.0,
            "y": 70.0,
            "width": 60.0,
            "height": 12.0,
            "strength": 14.0,
            "opacity": 0.85,
            "feather": 2.0,
            "corner_radius": 18.0,
            "color": "#000000",
        }
    ]
    assert (tmp_path / "jobs" / f"{submitted.id}.json").is_file()
    manager.shutdown()
