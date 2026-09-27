"""Cancellable metadata extraction in an isolated child process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

from .video_downloads import VideoDownloadManager


def extract_metadata(request: dict, cancellation: threading.Event) -> list[dict]:
    from .acquisition import AcquisitionError

    if cancellation.is_set():
        return []
    deadline = time.monotonic() + float(request.get("limits", {}).get("deadline_seconds", 300))
    process = subprocess.Popen(
        [sys.executable, "-m", "app.services.acquisition_worker"],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    payload = json.dumps(request)
    try:
        while True:
            if cancellation.is_set():
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcquisitionError("Lượt lấy metadata vượt thời gian cho phép.", code="DEADLINE_EXCEEDED")
            try:
                output, _ = process.communicate(input=payload, timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
        if cancellation.is_set():
            return []
        try:
            response = json.loads(output)
            if not isinstance(response, dict):
                raise TypeError
            if response.get("error"):
                raise AcquisitionError(
                    str(response["error"])[:500],
                    code=str(response.get("code") or "SOURCE_UNAVAILABLE"),
                )
            entries = response.get("items")
            if process.returncode or not isinstance(entries, list) or len(entries) > 100:
                raise ValueError
            if not all(isinstance(item, dict) for item in entries):
                raise ValueError
            return entries
        except (ValueError, TypeError) as error:
            if isinstance(error, AcquisitionError):
                raise
            raise AcquisitionError("Worker metadata không trả kết quả hợp lệ.", code="SOURCE_UNAVAILABLE") from error
    finally:
        with suppress(Exception):
            VideoDownloadManager._kill(process)
        with suppress(Exception):
            process.communicate(timeout=5)
