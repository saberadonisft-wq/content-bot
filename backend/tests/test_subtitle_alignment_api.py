from __future__ import annotations

import time
from pathlib import Path

from app.api import subtitles as subtitles_api
from app.schemas import SubtitleAlignmentRequest, SubtitleJobResponse
from app.services.subtitle_jobs import SubtitleJobManager


def test_alignment_endpoint_runs_as_attachable_job(
    application_services,
    tmp_path: Path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fixture")
    manager = SubtitleJobManager(tmp_path / "jobs", max_workers=1)
    media = {
        "fingerprint": "1" * 64,
        "audio_hash": "2" * 64,
        "duration_ms": 5000,
        "has_audio": True,
    }

    monkeypatch.setattr(application_services, "subtitle_jobs", manager)
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda _video_id: video_path)
    monkeypatch.setattr(subtitles_api, "probe_media_cached", lambda *_args, **_kwargs: media)

    def fake_align(_path, document, _media, **kwargs):
        kwargs["progress"](50, "audio", "Đang căn audio")
        next_document = {**document, "timing_source": "forced_alignment"}
        next_document["segments"] = [
            {
                **document["segments"][0],
                "start_ms": 940,
                "end_ms": 2110,
                "speech_start_ms": 1000,
                "speech_end_ms": 2000,
                "timing_source": "forced_alignment",
                "timing_precision_ms": 10,
                "confidence": 0.8,
            }
        ]
        return {
            "document": next_document,
            "warnings": [],
            "engine": "energy",
            "cache_hit": False,
            "aligned_cue_count": 1,
        }

    monkeypatch.setattr(subtitles_api, "align_subtitle_document", fake_align)
    request = SubtitleAlignmentRequest(
        video_id="a" * 12,
        document={
            "schema_version": 2,
            "language": "vi",
            "timebase": "milliseconds",
            "timing_source": "gemini_estimate",
            "timing_precision_ms": 1000,
            "segments": [
                {
                    "id": "s1",
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "text": "Xin chào",
                }
            ],
        },
    )

    submitted = subtitles_api.align_subtitle_timeline_v2_endpoint(
        request, services=application_services
    )
    validated_submission = SubtitleJobResponse(**submitted)
    assert validated_submission.kind == "alignment"

    deadline = time.monotonic() + 3
    completed = None
    while time.monotonic() < deadline:
        completed = manager.get(validated_submission.id)
        if completed and completed["state"] == "succeeded":
            break
        time.sleep(0.01)

    assert completed is not None
    validated = SubtitleJobResponse(**completed)
    assert validated.state == "succeeded"
    assert validated.result is not None
    assert validated.result["document"]["segments"][0]["start_ms"] == 940
    manager.shutdown()
