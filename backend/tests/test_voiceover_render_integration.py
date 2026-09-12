from app.api import subtitles as subtitles_api

"""Real FFmpeg path through the studio render endpoint, isolated from user files."""

import array
import subprocess
import threading
from types import SimpleNamespace

import imageio_ffmpeg
import pytest
from fastapi import HTTPException
from test_voiceover import asset, document

from app import main
from app.schemas import SubtitleRenderRequestV2
from app.services.media_probe import probe_media
from app.services.voiceover.mix import ffmpeg
from app.services.voiceover.store import VoiceStore


@pytest.mark.parametrize("with_subtitles", [False, True])
def test_studio_render_endpoint_exports_voice_on_silent_video(
    application_services, tmp_path, monkeypatch, with_subtitles
):
    source = tmp_path / "source.mp4"
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=25:d=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ]
    )
    media = probe_media(source)
    store = VoiceStore(tmp_path / "voice")
    doc = document()
    doc.video_fingerprint = media["fingerprint"]
    doc = store.save_document("user", doc)
    store.attach("user", doc.project_id, "one", asset(store, doc))
    doc = store.get_document("user", doc.project_id)
    output = tmp_path / "videos/output"
    output.mkdir(parents=True)
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda video_id: source)
    monkeypatch.setattr(subtitles_api, "probe_media_cached", lambda *a, **kw: media)
    monkeypatch.setattr(application_services.voiceover_manager, "store", store)
    monkeypatch.setattr(main.settings, "content_bot_data_dir", tmp_path)
    updates = []
    context = SimpleNamespace(
        cancel_event=threading.Event(), update=lambda *args: updates.append(args)
    )
    monkeypatch.setattr(
        application_services.subtitle_jobs,
        "submit",
        lambda kind, key, run: run(context),
    )
    request = SubtitleRenderRequestV2.model_validate(
        {
            "video_id": doc.project_id,
            "voice_project_id": doc.project_id,
            "voice_revision": doc.revision,
            "document": {
                "segments": [
                    {"id": "cue", "start_ms": 1000, "end_ms": 3000, "text": "Xin chào"}
                ]
                if with_subtitles
                else []
            },
            "options": {"trim_start_ms": 500, "trim_end_ms": 3500, "video_speed": 0.5},
        }
    )
    result = subtitles_api.render_subtitle_timeline_v2_endpoint(
        request, {"sub": "user"}, services=application_services
    )
    final = output / result["output_filename"]
    rendered = probe_media(final)
    assert rendered["has_audio"] and rendered["audio_codec"] == "aac"
    assert abs(rendered["duration_ms"] - 6000) <= 40
    assert updates[-1][0] == 100
    assert any(update[1] == "voiceover" for update in updates)
    decoded = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-v",
            "error",
            "-i",
            str(final),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    samples = array.array("h", decoded.stdout)
    onset = next(i for i, value in enumerate(samples) if abs(value) > 400) / 48000
    assert abs(onset - 1) <= 0.020
    if with_subtitles:
        frame = subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-v",
                "error",
                "-ss",
                "2",
                "-i",
                str(final),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        assert max(frame.stdout) > 100, "Subtitle pixels missing on black source"
    with pytest.raises(HTTPException) as stale:
        subtitles_api.render_subtitle_timeline_v2_endpoint(
            request.model_copy(update={"voice_revision": 0}),
            {"sub": "user"},
            services=application_services,
        )
    assert stale.value.status_code == 422
    with pytest.raises(HTTPException) as other_owner:
        subtitles_api.render_subtitle_timeline_v2_endpoint(
            request, {"sub": "another"}, services=application_services
        )
    assert other_owner.value.status_code == 422
    invalid_options = request.options.model_copy(
        update={"trim_start_ms": 5000, "trim_end_ms": 6000}
    )
    with pytest.raises(HTTPException) as empty_cut:
        subtitles_api.render_subtitle_timeline_v2_endpoint(
            request.model_copy(update={"options": invalid_options}),
            {"sub": "user"},
            services=application_services,
        )
    assert empty_cut.value.status_code == 422
    assert "empty" in empty_cut.value.detail


def test_studio_render_http_flow(application_services, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.middleware.auth import get_current_user

    source = tmp_path / "source.mp4"
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=25:d=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ]
    )
    media = probe_media(source)
    store = VoiceStore(tmp_path / "voice")
    doc = document()
    doc.video_fingerprint = media["fingerprint"]
    doc = store.save_document("user", doc)
    store.attach("user", doc.project_id, "one", asset(store, doc))
    doc = store.get_document("user", doc.project_id)
    output = tmp_path / "videos/output"
    output.mkdir(parents=True)

    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda video_id: source)
    monkeypatch.setattr(subtitles_api, "probe_media_cached", lambda *a, **kw: media)
    monkeypatch.setattr(application_services.voiceover_manager, "store", store)
    monkeypatch.setattr(main.settings, "content_bot_data_dir", tmp_path)

    payload = {
        "video_id": doc.project_id,
        "voice_project_id": doc.project_id,
        "voice_revision": doc.revision,
        "document": {
            "segments": [
                {"id": "cue", "start_ms": 1000, "end_ms": 3000, "text": "Xin chào"}
            ]
        },
        "options": {"trim_start_ms": 500, "trim_end_ms": 3500, "video_speed": 0.5},
    }

    main.app.dependency_overrides[get_current_user] = lambda: {"sub": "user"}
    try:
        with TestClient(main.app) as client:
            # 1. Different owner returns 422
            main.app.dependency_overrides[get_current_user] = lambda: {"sub": "another"}
            res_owner = client.post("/api/v1/subtitles/v2/render", json=payload)
            assert res_owner.status_code == 422

            # 2. Stale voice revision returns 422
            main.app.dependency_overrides[get_current_user] = lambda: {"sub": "user"}
            stale_payload = {**payload, "voice_revision": 0}
            res_stale = client.post("/api/v1/subtitles/v2/render", json=stale_payload)
            assert res_stale.status_code == 422

            # 3. Successful submit through HTTP
            res_submit = client.post("/api/v1/subtitles/v2/render", json=payload)
            assert res_submit.status_code == 200
            job_info = res_submit.json()
            assert "id" in job_info
            job_id = job_info["id"]

            # 4. Query job through HTTP
            res_job = client.get(f"/api/v1/subtitles/jobs/{job_id}")
            assert res_job.status_code == 200
            assert res_job.json()["id"] == job_id

            # 5. Cancel endpoint through HTTP
            res_cancel = client.post(f"/api/v1/subtitles/jobs/{job_id}/cancel")
            assert res_cancel.status_code == 200
            assert res_cancel.json()["id"] == job_id
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)
