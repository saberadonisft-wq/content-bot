import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.subtitles as subtitles_api
from app.application_services import get_services
from app.services.subtitle_jobs import SubtitleJobManager


def test_scene_detection_is_a_cancelable_job_and_keeps_video_bounds(tmp_path, monkeypatch):
    video_id = "a" * 32
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda value: input_path)
    monkeypatch.setattr(
        subtitles_api,
        "probe_media_cached",
        lambda *args, **kwargs: {
            "fingerprint": "f" * 64,
            "duration_ms": 5000,
            "width": 320,
            "height": 180,
        },
    )

    def fake_detect(path, **kwargs):
        assert path == input_path
        kwargs["check_cancel"]()
        kwargs["progress"](55, "detecting", "fixture")
        return [0.0, 2.0, 3.5, 5.0]

    monkeypatch.setattr(subtitles_api, "detect_scene_cuts", fake_detect)
    jobs = SubtitleJobManager(tmp_path / "jobs")
    app = FastAPI()
    app.include_router(subtitles_api.router)
    app.dependency_overrides[get_services] = lambda: SimpleNamespace(subtitle_jobs=jobs)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/subtitles/v2/scene-detect",
                json={
                    "video_id": video_id,
                    "target_duration_s": 5,
                    "min_duration_s": 1,
                    "max_duration_s": 6,
                },
            )
            assert response.status_code == 200, response.text
            submitted = response.json()
            assert submitted["kind"] == "scene"
            assert submitted["details"] == {}

            deadline = time.monotonic() + 3
            record = jobs.get(submitted["id"])
            while record and record["state"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
                record = jobs.get(submitted["id"])
            assert record and record["state"] == "succeeded", record
            result = record["result"]
            assert result["scene_cuts_s"][0] == 0
            assert result["scene_cuts_s"][-1] == 5
            assert result["chunks"][0]["start_ms"] == 0
            assert result["chunks"][-1]["end_ms"] == 5000
            assert client.post(
                "/api/v1/subtitles/v2/scene-detect",
                json={"video_id": video_id, "min_duration_s": 50, "target_duration_s": 20},
            ).status_code == 422
    finally:
        jobs.shutdown(wait=True)


def test_scene_export_checks_fingerprint_and_publishes_manifest_urls(tmp_path, monkeypatch):
    video_id = "b" * 32
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(subtitles_api.settings, "content_bot_data_dir", tmp_path)
    output_dir = tmp_path / "videos" / "shorts" / video_id
    output_dir.mkdir(parents=True)
    manifest = output_dir / f"{video_id}_manifest.json"
    video = output_dir / f"{video_id}_short_01.mp4"
    srt = output_dir / f"{video_id}_short_01.srt"
    document = output_dir / f"{video_id}_short_01.json"
    for path, content in ((manifest, b"{}"), (video, b"video"), (srt, b"1\n"), (document, b"{}")):
        path.write_bytes(content)
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda value: input_path)
    monkeypatch.setattr(
        subtitles_api,
        "probe_media_cached",
        lambda *args, **kwargs: {"fingerprint": "f" * 64, "duration_ms": 5000, "width": 320, "height": 180},
    )
    monkeypatch.setattr(
        subtitles_api,
        "export_scene_chunks",
        lambda *args, **kwargs: [{
            "chunk_index": 1, "start_s": 0, "end_s": 5, "video_file": video.name,
            "srt_file": srt.name, "subtitles_file": document.name,
            "manifest_path": str(manifest),
        }],
    )
    jobs = SubtitleJobManager(tmp_path / "jobs")
    app = FastAPI()
    app.include_router(subtitles_api.router)
    app.dependency_overrides[get_services] = lambda: SimpleNamespace(subtitle_jobs=jobs)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/subtitles/v2/scene-export",
                json={
                    "video_id": video_id,
                    "source_fingerprint": "f" * 64,
                    "chunks": [{"id": "scene-1", "start_ms": 0, "end_ms": 5000}],
                },
            )
            assert response.status_code == 200, response.text
            submitted = response.json()
            deadline = time.monotonic() + 3
            record = jobs.get(submitted["id"])
            while record and record["state"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
                record = jobs.get(submitted["id"])
            assert record and record["state"] == "succeeded", record
            result = record["result"]
            assert result["files"][0]["video_url"].endswith(video.name)
            assert client.get(result["manifest_url"]).content == b"{}"
            assert client.get(result["files"][0]["srt_url"]).content == b"1\n"
            publication = output_dir / '.publications' / f'{manifest.name}.json'
            assert publication.is_file()
            assert client.get(result['files'][0]['video_url']).status_code == 200
            video.write_bytes(b'replaced')
            assert client.get(result['files'][0]['video_url']).status_code == 409
            video.write_bytes(b'video')
            (output_dir / '.publications' / f'{manifest.name}.json').unlink()
            assert client.get(result['manifest_url']).status_code == 409
            stale = client.post(
                "/api/v1/subtitles/v2/scene-export",
                json={
                    "video_id": video_id,
                    "source_fingerprint": "0" * 64,
                    "chunks": [{"id": "scene-1", "start_ms": 0, "end_ms": 5000}],
                },
            )
            assert stale.status_code == 409
    finally:
        jobs.shutdown(wait=True)


def test_scene_detection_does_not_publish_after_source_replacement(tmp_path, monkeypatch):
    video_id = "c" * 32
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda value: input_path)
    fingerprints = iter(("f" * 64, "0" * 64))

    def probe(*args, **kwargs):
        return {
            "fingerprint": next(fingerprints),
            "duration_ms": 5000,
            "width": 320,
            "height": 180,
        }

    monkeypatch.setattr(subtitles_api, "probe_media_cached", probe)
    monkeypatch.setattr(
        subtitles_api,
        "detect_scene_cuts",
        lambda _path, **kwargs: (kwargs["check_cancel"](), [0.0, 5.0])[1],
    )
    jobs = SubtitleJobManager(tmp_path / "jobs")
    app = FastAPI()
    app.include_router(subtitles_api.router)
    app.dependency_overrides[get_services] = lambda: SimpleNamespace(subtitle_jobs=jobs)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/subtitles/v2/scene-detect",
                json={"video_id": video_id, "target_duration_s": 5, "min_duration_s": 1},
            )
            assert response.status_code == 200
            submitted = response.json()
            deadline = time.monotonic() + 3
            record = jobs.get(submitted["id"])
            while record and record["state"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
                record = jobs.get(submitted["id"])
            assert record and record["state"] == "failed", record
            assert "Video nguồn đã thay đổi" in (record["error"] or "")
            assert record["result"] is None
    finally:
        jobs.shutdown(wait=True)
