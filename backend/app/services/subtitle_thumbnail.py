from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path
from typing import Any

import imageio_ffmpeg

THUMBNAIL_SPRITE_VERSION = "2026-08-v1"
SPRITE_FRAME_WIDTH = 120
SPRITE_FRAME_HEIGHT = 68
_SPRITE_LOCK = threading.Lock()


class ThumbnailSpriteError(RuntimeError):
    pass


def thumbnail_frame_count(duration_ms: int) -> int:
    return min(16, max(8, (max(1, duration_ms) + 7_999) // 8_000))


def generate_thumbnail_sprite(
    video_path: Path,
    media: dict[str, Any],
    cache_dir: Path,
    *,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    if not video_path.is_file():
        raise ThumbnailSpriteError("Video file does not exist")
    duration_ms = int(media["duration_ms"])
    frame_count = thumbnail_frame_count(duration_ms)
    fingerprint = str(media.get("fingerprint") or video_path.stat().st_size)
    cache_key = hashlib.sha256(
        f"{THUMBNAIL_SPRITE_VERSION}:{fingerprint}:{frame_count}".encode()
    ).hexdigest()[:20]
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / f"sprite-{cache_key}.jpg"
    if output.is_file() and output.stat().st_size > 0:
        return {
            "path": output,
            "frame_count": frame_count,
            "frame_width": SPRITE_FRAME_WIDTH,
            "frame_height": SPRITE_FRAME_HEIGHT,
            "cache_hit": True,
        }

    with _SPRITE_LOCK:
        if output.is_file() and output.stat().st_size > 0:
            return {
                "path": output,
                "frame_count": frame_count,
                "frame_width": SPRITE_FRAME_WIDTH,
                "frame_height": SPRITE_FRAME_HEIGHT,
                "cache_hit": True,
            }
        part = cache_dir / f"{output.stem}.part.jpg"
        part.unlink(missing_ok=True)
        duration_seconds = max(0.001, duration_ms / 1000)
        sample_rate = frame_count / duration_seconds
        filters = (
            f"fps={sample_rate:.9f},"
            f"scale={SPRITE_FRAME_WIDTH}:{SPRITE_FRAME_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={SPRITE_FRAME_WIDTH}:{SPRITE_FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"tile={frame_count}x1"
        )
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path.resolve()),
            "-an",
            "-vf",
            filters,
            "-frames:v",
            "1",
            "-q:v",
            "5",
            "-threads",
            "1",
            str(part.resolve()),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
                raise ThumbnailSpriteError(
                    result.stderr[-2000:] or "FFmpeg did not create a thumbnail sprite"
                )
            part.replace(output)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ThumbnailSpriteError("Thumbnail sprite generation failed") from exc
        finally:
            part.unlink(missing_ok=True)

    return {
        "path": output,
        "frame_count": frame_count,
        "frame_width": SPRITE_FRAME_WIDTH,
        "frame_height": SPRITE_FRAME_HEIGHT,
        "cache_hit": False,
    }
