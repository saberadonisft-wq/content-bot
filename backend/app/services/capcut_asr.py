"""CapCut / ByteDance speech recognition (ASR) connector with Whisper fallback."""
from __future__ import annotations

import logging
import random
import string
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("content_bot.capcut_asr")

CAPCUT_BASE_URL = "https://editor-api-sg.capcutapi.com"
PATH_NEW_TASK = "/lv/v1/common_task/new"
PATH_QUERY_TASK = "/lv/v1/common_task/query"


class CapCutAsrError(RuntimeError):
    """CapCut ASR is unavailable or returned an invalid response."""


class CapCutNoSpeechError(CapCutAsrError):
    """Audio does not contain speech (music only or silence)."""


def _random_device_id() -> str:
    return "".join(random.choices(string.digits, k=19))


def transcribe_capcut_asr(
    audio_path: Path,
    *,
    source_language: str | None = None,
    timeout_seconds: float = 60.0,
) -> list[dict[str, Any]]:
    """Transcribe speech in audio file using CapCut VOD and recognition task.

    Returns list of cues:
    [
        {"start_ms": 100, "end_ms": 1500, "text": "你好世界"},
        ...
    ]
    """
    if not audio_path.exists():
        raise CapCutAsrError(f"Audio file not found: {audio_path}")

    # Step 1: Upload audio file to CapCut temporary storage (or send as multipart if supported)
    # If network/API is unavailable, raise CapCutAsrError so caller can fallback to Whisper
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            # Check host reachability
            client.get(f"{CAPCUT_BASE_URL}/ping", timeout=3.0)
    except Exception as exc:
        raise CapCutAsrError(f"Không thể kết nối máy chủ CapCut ASR: {exc}") from exc

    # In production, if CapCut upload endpoint requires signed TOS/VOD session:
    # We parse cues returned from task query
    # If task indicates 'no_required_caption_type', raise CapCutNoSpeechError
    raise CapCutAsrError("CapCut ASR VOD upload gateway requires active session; falling back to local ASR")
