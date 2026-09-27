from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import main


def test_select_thumbnail_endpoint_writes_owned_selected_thumbnail(
    tmp_path: Path, application_services, monkeypatch
) -> None:
    monkeypatch.setattr(main.settings, "content_bot_data_dir", tmp_path)
    monkeypatch.setattr(main.settings, "content_bot_auth_enabled", False)
    video_id = "a" * 32
    upload = tmp_path / "videos" / "upload"
    upload.mkdir(parents=True)
    source = upload / f"{video_id}.mp4"
    source.write_bytes(b"fixture video")

    def fake_selector(video_path, output_path, *, n_candidates, anti_duplicate):
        assert video_path == source
        assert n_candidates == 9
        assert anti_duplicate is True
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"selected jpeg")
        return {"thumbnail_path": str(output_path), "best_timestamp_s": 1.5, "score": 8.0}

    monkeypatch.setattr(main.videos, "select_best_thumbnail", fake_selector)
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/videos/{video_id}/thumbnail/select",
            params={"n_candidates": 9, "anti_duplicate": "true"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["best_timestamp_s"] == 1.5
        assert payload["thumbnail_url"].endswith("type=selected")

        image = client.get(payload["thumbnail_url"])
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/jpeg")
        assert image.content == b"selected jpeg"
        listed = client.get("/api/v1/videos").json()
        local = next(item for item in listed if item["id"] == video_id and item["type"] == "original")
        assert "type=selected" in local["thumbnail_url"]


def test_selected_thumbnail_rejects_path_traversal(application_services) -> None:
    with TestClient(main.app) as client:
        response = client.get(
            "/api/v1/videos/../outside/thumbnail", params={"type": "selected"}
        )
    assert response.status_code in {404, 422}
