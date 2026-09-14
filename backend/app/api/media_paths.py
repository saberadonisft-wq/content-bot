from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import HTTPException

from ..config import settings

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv"}
VIDEO_ID_PATTERN = re.compile(r"^[a-f0-9]{12,32}$")
SUBTITLED_VIDEO_ID_PATTERN = re.compile(r"^[a-f0-9]{12,32}(?:_[a-f0-9]{12})?$")

logger = logging.getLogger(__name__)


def _validate_local_video_id(video_id: str) -> None:
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise HTTPException(status_code=422, detail="Invalid video id")


def _subtitled_video_path(video_id: str) -> Path:
    # Precision renders have a suffix identifying this exact exported version.
    # Keep it intact: stripping it could select/delete a different render.
    if not SUBTITLED_VIDEO_ID_PATTERN.fullmatch(video_id):
        raise HTTPException(status_code=422, detail="Invalid subtitled video id")
    return settings.data_dir / "videos" / "output" / f"subtitled_{video_id}.mp4"


def _uploaded_video_path(video_id: str) -> Path:
    _validate_local_video_id(video_id)
    upload_dir = settings.data_dir / "videos" / "upload"
    matches = sorted(
        path
        for path in upload_dir.glob(f"{video_id}.*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
    )
    if not matches:
        raise HTTPException(status_code=404, detail="Video file not found")
    return matches[0]
