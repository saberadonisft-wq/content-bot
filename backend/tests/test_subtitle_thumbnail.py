from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

from app.api import subtitles as subtitles_api
from app.services.media_probe import probe_media
from app.services.subtitle_thumbnail import (
    SPRITE_FRAME_HEIGHT,
    SPRITE_FRAME_WIDTH,
    generate_thumbnail_sprite,
    thumbnail_frame_count,
)
from app.services.video_thumbnails import VideoThumbnails


def test_thumbnail_frame_count_is_bounded() -> None:
    assert thumbnail_frame_count(1_000) == 8
    assert thumbnail_frame_count(80_000) == 10
    assert thumbnail_frame_count(10_000_000) == 16


def test_ffmpeg_thumbnails_are_atomic_and_cached(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
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
            "testsrc2=s=320x180:r=24:d=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)

    first = generate_thumbnail_sprite(source, media, tmp_path / "cache")
    second = generate_thumbnail_sprite(source, media, tmp_path / "cache")

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["frame_count"] == 8
    assert first["frame_width"] == SPRITE_FRAME_WIDTH
    assert first["frame_height"] == SPRITE_FRAME_HEIGHT
    assert first["path"].is_file()
    assert first["path"].stat().st_size > 0
    assert not list(tmp_path.rglob("*.part.*"))

    thumbnails = VideoThumbnails(timeout_seconds=10)
    output = tmp_path / "library.jpg"
    try:
        assert thumbnails.get(source, output) == output
        assert output.read_bytes().startswith(b"\xff\xd8")
        modified = output.stat().st_mtime_ns
        assert thumbnails.get(source, output) == output
        assert output.stat().st_mtime_ns == modified
        assert not list(tmp_path.rglob("*.part.*"))
    finally:
        thumbnails.shutdown()


def test_thumbnail_sprite_endpoint_exposes_layout_headers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    sprite_path = tmp_path / "sprite.jpg"
    sprite_path.write_bytes(b"jpeg")
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda _video_id: source)
    monkeypatch.setattr(
        subtitles_api, "probe_media_cached", lambda *_args, **_kwargs: {"duration_ms": 1000}
    )
    monkeypatch.setattr(
        subtitles_api,
        "generate_thumbnail_sprite",
        lambda *_args, **_kwargs: {
            "path": sprite_path,
            "frame_count": 8,
            "frame_width": 120,
            "frame_height": 68,
            "cache_hit": True,
        },
    )

    response = subtitles_api.get_subtitle_thumbnail_sprite("fixture-video")

    assert Path(response.path) == sprite_path
    assert response.media_type == "image/jpeg"
    assert response.headers["x-sprite-frames"] == "8"
    assert response.headers["x-sprite-frame-width"] == "120"
    assert response.headers["x-sprite-frame-height"] == "68"
    assert response.headers["x-cache-hit"] == "1"
