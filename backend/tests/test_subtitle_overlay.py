from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
from fastapi.testclient import TestClient

from app import main
from app.services.subtitle_overlay import (
    SubtitleOverlayError,
    resolve_subtitle_overlay,
    validate_subtitle_overlay,
)


def _create_png(path: Path, *, size: str = "48x24") -> None:
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s={size}:d=0.1,format=rgba",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-threads",
            "1",
            "-update",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_overlay_validation_checks_signature_and_decodability(tmp_path: Path) -> None:
    valid = tmp_path / "logo.png"
    _create_png(valid)
    validate_subtitle_overlay(valid)

    invalid = tmp_path / "fake.png"
    invalid.write_bytes(b"not a png")
    try:
        validate_subtitle_overlay(invalid)
    except SubtitleOverlayError as exc:
        assert "extension" in str(exc)
    else:
        raise AssertionError("Invalid image was accepted")


def test_overlay_upload_is_content_addressed_and_servable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source-logo.png"
    _create_png(source)
    monkeypatch.setattr(main.settings, "content_bot_data_dir", tmp_path / "data")

    with TestClient(main.app) as client:
        first = client.post(
            "/api/v1/subtitles/overlays",
            files={"file": ("logo.png", source.read_bytes(), "image/png")},
        )
        second = client.post(
            "/api/v1/subtitles/overlays",
            files={"file": ("renamed.png", source.read_bytes(), "image/png")},
        )

        assert first.status_code == 200
        assert second.status_code == 200
        payload = first.json()
        assert payload["overlay_id"] == second.json()["overlay_id"]
        assert payload["size_bytes"] == source.stat().st_size
        stored = resolve_subtitle_overlay(
            main.settings.data_dir,
            payload["overlay_id"],
        )
        assert stored.read_bytes() == source.read_bytes()

        served = client.get(payload["overlay_url"])
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("image/png")
        assert served.content == source.read_bytes()

    stored_files = list((tmp_path / "data" / "subtitle-overlays").glob("*"))
    assert stored_files == [stored]
