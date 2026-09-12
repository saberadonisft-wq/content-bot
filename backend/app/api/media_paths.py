from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import HTTPException

from ..config import settings

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv"}
VIDEO_ID_PATTERN = re.compile(r"^[a-f0-9]{12,32}$")

logger = logging.getLogger(__name__)


def _validate_local_video_id(video_id: str) -> None:
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise HTTPException(status_code=422, detail="Invalid video id")


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
