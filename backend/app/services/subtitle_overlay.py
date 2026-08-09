from __future__ import annotations

import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

SUPPORTED_OVERLAY_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
OVERLAY_ID_PATTERN = re.compile(r"^[a-f0-9]{64}$")
OVERLAY_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class SubtitleOverlayError(RuntimeError):
    pass


def overlay_directory(data_dir: Path) -> Path:
    return data_dir / "subtitle-overlays"


def resolve_subtitle_overlay(data_dir: Path, overlay_id: str) -> Path:
    if not OVERLAY_ID_PATTERN.fullmatch(overlay_id):
        raise SubtitleOverlayError("Invalid overlay id")
    directory = overlay_directory(data_dir)
    matches = sorted(
        path
        for path in directory.glob(f"{overlay_id}.*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_OVERLAY_EXTENSIONS
    )
    if not matches:
        raise SubtitleOverlayError("Overlay image not found")
    return matches[0]


def _has_expected_signature(path: Path) -> bool:
    with path.open("rb") as image_file:
        header = image_file.read(16)
    extension = path.suffix.lower()
    if extension == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False


def validate_subtitle_overlay(path: Path, *, timeout_seconds: int = 30) -> None:
    if path.suffix.lower() not in SUPPORTED_OVERLAY_EXTENSIONS:
        raise SubtitleOverlayError("Unsupported overlay image extension")
    if not _has_expected_signature(path):
        raise SubtitleOverlayError("Overlay content does not match its extension")
    try:
        result = subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path.resolve()),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SubtitleOverlayError("Could not validate overlay image") from exc
    if result.returncode != 0:
        raise SubtitleOverlayError("The uploaded overlay is not a valid image")


def overlay_media_type(path: Path) -> str:
    return OVERLAY_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
