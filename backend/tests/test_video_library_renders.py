from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "content_bot_data_dir", tmp_path)
    monkeypatch.setattr(main.settings, "content_bot_auth_enabled", False)
    output = tmp_path / "videos" / "output"
    upload = tmp_path / "videos" / "upload"
    output.mkdir(parents=True)
    upload.mkdir(parents=True)
    with TestClient(main.app) as client:
        yield client, output, upload


@pytest.mark.parametrize("suffix", ["", "_096d1e842365"])
def test_library_can_view_thumbnail_and_delete_exact_render(library, application_services, monkeypatch, suffix):
    client, output, upload = library
    source_id = "ba24206d3674497785af4c59e659b85f"
    render_id = source_id + suffix
    selected = output / f"subtitled_{render_id}.mp4"
    sibling = output / f"subtitled_{source_id}_aaaaaaaaaaaa.mp4"
    source = upload / f"{source_id}.mp4"
    selected.write_bytes(b"selected render")
    sibling.write_bytes(b"other render")
    source.write_bytes(b"original video")
    legacy = output / f"subtitled_{source_id}.mp4"
    if suffix:
        legacy.write_bytes(b"legacy render")

    def thumbnail(video_path, thumbnail_path):
        assert video_path == selected
        thumbnail_path.write_bytes(b"thumbnail")
        thumbnail_path.with_suffix(".fingerprint").write_text("fixture")

    monkeypatch.setattr(application_services.video_thumbnails, "get", thumbnail)
    videos = client.get("/api/v1/videos").json()
    video = next(item for item in videos if item["id"] == render_id and item["type"] == "subtitled")
    assert client.get(video["video_url"]).content == b"selected render"
    assert client.get(video["thumbnail_url"]).content == b"thumbnail"
    response = client.delete(f"/api/v1/videos/{render_id}", params={"type": "subtitled"})
    assert response.status_code == 204
    assert not selected.exists()
    assert sibling.read_bytes() == b"other render"
    assert source.read_bytes() == b"original video"
    if suffix:
        assert legacy.read_bytes() == b"legacy render"
    thumbnails = output.parent / "thumbnails"
    assert not (thumbnails / f"{render_id}_subtitled.jpg").exists()
    assert not (thumbnails / f"{render_id}_subtitled.fingerprint").exists()
    assert all(item["id"] != render_id or item["type"] != "subtitled" for item in client.get("/api/v1/videos").json())
    assert client.delete(f"/api/v1/videos/{render_id}", params={"type": "subtitled"}).status_code == 404


@pytest.mark.parametrize("video_id", [
    "a" * 32 + "_too-short", "a" * 32 + "_" + "b" * 13,
    "a" * 32 + "_" + "b" * 12 + "_" + "c" * 12, "..\\outside",
])
def test_subtitle_routes_reject_invalid_render_ids(library, video_id):
    client, _, _ = library
    assert client.delete(f"/api/v1/videos/{video_id}", params={"type": "subtitled"}).status_code in {404, 422}
    assert client.get(f"/api/v1/videos/{video_id}/thumbnail", params={"type": "subtitled"}).status_code in {404, 422}
    assert client.get(f"/api/v1/subtitles/video/{video_id}", params={"type": "subtitled"}).status_code in {404, 422}


def test_render_suffix_is_not_accepted_as_an_original_video_id(library):
    client, _, upload = library
    render_id = "a" * 32 + "_" + "b" * 12
    path = upload / f"{render_id}.mp4"
    path.write_bytes(b"untouched")
    assert client.delete(f"/api/v1/videos/{render_id}", params={"type": "original"}).status_code == 422
    assert client.get(f"/api/v1/subtitles/video/{render_id}").status_code == 422
    assert path.read_bytes() == b"untouched"
