from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

import imageio_ffmpeg

MEDIA_PROBE_CACHE_VERSION = 1
_FRAME_TIME_BASE_RE = re.compile(r"^#tb\s+0:\s*(\d+)\s*/\s*(\d+)\s*$")
_FRAME_CODEC_RE = re.compile(r"^#codec_id\s+0:\s*([^\s]+)")
_FRAME_DIMENSIONS_RE = re.compile(r"^#dimensions\s+0:\s*(\d+)x(\d+)")
_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?),\s*start:\s*(-?\d+(?:\.\d+)?)"
)
_VIDEO_STREAM_RE = re.compile(r"Stream\s+#\S+:\s*Video:\s*([^,\s]+)", re.IGNORECASE)
_AUDIO_STREAM_RE = re.compile(
    r"Stream\s+#\S+:\s*Audio:\s*([^,\s]+).*?,\s*(\d+)\s*Hz,\s*([^,\r\n]+)",
    re.IGNORECASE,
)
_ROTATION_RE = re.compile(r"rotation of\s+(-?\d+(?:\.\d+)?)\s+degrees", re.IGNORECASE)
_ROTATE_TAG_RE = re.compile(r"^\s*rotate\s*:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE)
_HASH_RE = re.compile(r"SHA256=([0-9a-fA-F]{64})")


class MediaProbeError(RuntimeError):
    pass


def _quick_fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}:".encode("ascii"))
    sample_size = 1024 * 1024
    with path.open("rb") as source:
        digest.update(source.read(sample_size))
        if stat.st_size > sample_size:
            source.seek(max(0, stat.st_size - sample_size))
            digest.update(source.read(sample_size))
    return digest.hexdigest()


def _duration_from_stderr(stderr: str) -> tuple[int | None, int]:
    match = _DURATION_RE.search(stderr)
    if not match:
        return None, 0
    hours, minutes, seconds, start_seconds = match.groups()
    duration_ms = round(
        (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
    )
    return duration_ms, round(float(start_seconds) * 1000)


def _audio_channels(description: str) -> int | None:
    normalized = description.strip().lower()
    if normalized == "mono":
        return 1
    if normalized == "stereo":
        return 2
    surround = re.search(r"(\d+)\.(\d+)", normalized)
    if surround:
        return int(surround.group(1)) + int(surround.group(2))
    channels = re.search(r"(\d+)\s+channels?", normalized)
    return int(channels.group(1)) if channels else None


def _rotation_from_stderr(stderr: str) -> int:
    match = _ROTATION_RE.search(stderr) or _ROTATE_TAG_RE.search(stderr)
    if not match:
        return 0
    rotation = round(float(match.group(1))) % 360
    return rotation if rotation <= 180 else rotation - 360


def _parse_framecrc(stdout: str, stderr: str) -> dict[str, Any]:
    time_base_numerator = 0
    time_base_denominator = 0
    width = 0
    height = 0
    video_codec = "unknown"
    packets: list[tuple[int, int]] = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if match := _FRAME_TIME_BASE_RE.match(line):
            time_base_numerator = int(match.group(1))
            time_base_denominator = int(match.group(2))
            continue
        if match := _FRAME_CODEC_RE.match(line):
            video_codec = match.group(1).lower()
            continue
        if match := _FRAME_DIMENSIONS_RE.match(line):
            width, height = int(match.group(1)), int(match.group(2))
            continue
        if line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 4 or fields[0] != "0":
            continue
        try:
            presentation_timestamp = int(fields[2])
            packet_duration = max(0, int(fields[3]))
        except ValueError:
            continue
        packets.append((presentation_timestamp, packet_duration))

    if not packets or time_base_numerator <= 0 or time_base_denominator <= 0:
        raise MediaProbeError("FFmpeg did not expose a usable video frame index")

    if width <= 0 or height <= 0:
        raise MediaProbeError("FFmpeg did not expose valid video dimensions")

    # Presentation timestamps may arrive in decode order when B-frames are present.
    durations_by_pts: dict[int, int] = {}
    for pts, duration in packets:
        durations_by_pts[pts] = max(duration, durations_by_pts.get(pts, 0))
    presentation_ticks = sorted(durations_by_pts)
    first_tick = presentation_ticks[0]
    relative_ticks = [pts - first_tick for pts in presentation_ticks]
    positive_deltas = [
        right - left
        for left, right in pairwise(presentation_ticks)
        if right > left
    ]

    if positive_deltas:
        modal_delta = Counter(positive_deltas).most_common(1)[0][0]
    else:
        modal_delta = max(1, durations_by_pts[presentation_ticks[0]])
    cadence_tolerance = max(1, round(modal_delta * 0.005))
    is_vfr = any(abs(delta - modal_delta) > cadence_tolerance for delta in positive_deltas)

    nominal_rate = Fraction(
        time_base_denominator,
        time_base_numerator * max(1, modal_delta),
    ).limit_denominator(1_000_000)
    last_duration_ticks = durations_by_pts[presentation_ticks[-1]] or modal_delta
    timeline_ticks = relative_ticks[-1] + last_duration_ticks
    timeline_duration_ms = max(
        1,
        round(timeline_ticks * time_base_numerator * 1000 / time_base_denominator),
    )
    if len(relative_ticks) > 1 and relative_ticks[-1] > 0:
        average_fps = (
            (len(relative_ticks) - 1)
            * time_base_denominator
            / (relative_ticks[-1] * time_base_numerator)
        )
    else:
        average_fps = float(nominal_rate)

    frame_pts_ms: list[int] = []
    if is_vfr:
        for ticks in relative_ticks:
            milliseconds = round(
                ticks * time_base_numerator * 1000 / time_base_denominator
            )
            if not frame_pts_ms or milliseconds > frame_pts_ms[-1]:
                frame_pts_ms.append(milliseconds)

    container_duration_ms, source_start_ms = _duration_from_stderr(stderr)
    duration_ms = max(timeline_duration_ms, container_duration_ms or 0)
    audio_match = _AUDIO_STREAM_RE.search(stderr)
    fallback_codec = _VIDEO_STREAM_RE.search(stderr)
    if video_codec == "unknown" and fallback_codec:
        video_codec = fallback_codec.group(1).lower()
    rotation = _rotation_from_stderr(stderr)
    if abs(rotation) == 90:
        width, height = height, width

    return {
        "duration_ms": duration_ms,
        "source_start_ms": source_start_ms,
        "time_base_numerator": time_base_numerator,
        "time_base_denominator": time_base_denominator,
        "frame_rate_numerator": nominal_rate.numerator,
        "frame_rate_denominator": nominal_rate.denominator,
        "average_fps": round(average_fps, 6),
        "frame_count": len(presentation_ticks),
        "is_vfr": is_vfr,
        "frame_pts_ms": frame_pts_ms,
        "frame_index_source": "packet_pts",
        "width": width,
        "height": height,
        "rotation": rotation,
        "video_codec": video_codec,
        "has_audio": audio_match is not None,
        "audio_codec": audio_match.group(1).lower() if audio_match else None,
        "audio_sample_rate": int(audio_match.group(2)) if audio_match else None,
        "audio_channels": _audio_channels(audio_match.group(3)) if audio_match else None,
    }


def _hash_audio_stream(path: Path, ffmpeg_exe: str, timeout_seconds: int) -> str | None:
    try:
        result = subprocess.run(
            [
                ffmpeg_exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path.resolve()),
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-f",
                "hash",
                "-hash",
                "sha256",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _HASH_RE.search(result.stdout)
    return match.group(1).lower() if match else None


def probe_media(path: Path, *, timeout_seconds: int = 120) -> dict[str, Any]:
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        result = subprocess.run(
            [
                ffmpeg_exe,
                "-hide_banner",
                "-i",
                str(path.resolve()),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "framecrc",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError("Media probe timed out") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError("The uploaded file is not a readable video") from exc

    metadata = _parse_framecrc(result.stdout, result.stderr)
    metadata["audio_hash"] = (
        _hash_audio_stream(path, ffmpeg_exe, timeout_seconds)
        if metadata["has_audio"]
        else None
    )
    metadata["fingerprint"] = _quick_fingerprint(path)
    metadata["file_size_bytes"] = path.stat().st_size
    return metadata


def probe_media_cached(
    path: Path,
    cache_dir: Path,
    *,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    fingerprint = _quick_fingerprint(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{fingerprint}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("cache_version") == MEDIA_PROBE_CACHE_VERSION
                and cached.get("metadata", {}).get("fingerprint") == fingerprint
            ):
                return cached["metadata"]
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    metadata = probe_media(path, timeout_seconds=timeout_seconds)
    payload = {
        "cache_version": MEDIA_PROBE_CACHE_VERSION,
        "metadata": metadata,
    }
    temporary_path = cache_path.with_suffix(".json.part")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary_path.replace(cache_path)
    return metadata


def rational_fps(metadata: dict[str, Any]) -> float:
    numerator = int(metadata.get("frame_rate_numerator") or 0)
    denominator = int(metadata.get("frame_rate_denominator") or 0)
    if numerator <= 0 or denominator <= 0:
        return 0.0
    value = numerator / denominator
    return value if math.isfinite(value) else 0.0
