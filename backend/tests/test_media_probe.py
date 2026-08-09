from __future__ import annotations

from app.schemas import MediaMetadata
from app.services.media_probe import _parse_framecrc


def _framecrc(*rows: str, time_base: str = "1/30000") -> str:
    return "\n".join(
        [
            "#software: Lavf61.7.100",
            f"#tb 0: {time_base}",
            "#media_type 0: video",
            "#codec_id 0: h264",
            "#dimensions 0: 1920x1080",
            *rows,
        ]
    )


def test_probe_parser_preserves_rational_cfr_cadence() -> None:
    stdout = _framecrc(
        "0, -2002, 0, 1001, 1, 0x0",
        "0, -1001, 2002, 1001, 1, 0x0",
        "0, 0, 1001, 1001, 1, 0x0",
    )
    stderr = """Duration: 00:00:00.100, start: 0.000000, bitrate: 1 kb/s
Stream #0:0: Video: h264, yuv420p, 1920x1080
Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp
"""

    media = _parse_framecrc(stdout, stderr)

    assert media["frame_rate_numerator"] == 30_000
    assert media["frame_rate_denominator"] == 1001
    assert media["frame_count"] == 3
    assert media["is_vfr"] is False
    assert media["frame_pts_ms"] == []
    assert media["duration_ms"] == 100
    assert media["audio_sample_rate"] == 48_000
    assert media["audio_channels"] == 2


def test_probe_parser_exposes_normalized_vfr_pts() -> None:
    stdout = _framecrc(
        "0, 0, 900, 900, 1, 0x0",
        "0, 900, 0, 900, 1, 0x0",
        "0, 1800, 2400, 1500, 1, 0x0",
    )
    stderr = "Duration: 00:00:00.130, start: 1.250000, bitrate: 1 kb/s"

    media = _parse_framecrc(stdout, stderr)

    assert media["is_vfr"] is True
    assert media["frame_pts_ms"] == [0, 30, 80]
    assert media["source_start_ms"] == 1250
    assert media["has_audio"] is False

    validated = MediaMetadata(
        **media,
        fingerprint="0" * 64,
        file_size_bytes=1,
        audio_hash=None,
    )
    assert validated.frame_pts_ms == [0, 30, 80]


def test_probe_parser_reports_display_dimensions_for_rotated_video() -> None:
    stdout = _framecrc(
        "0, 0, 0, 1001, 1, 0x0",
        "0, 1001, 1001, 1001, 1, 0x0",
    ).replace("1920x1080", "1280x720")
    stderr = """Duration: 00:00:00.067, start: 0.000000, bitrate: 1 kb/s
Stream #0:0: Video: h264, yuv420p, 1280x720
    displaymatrix: rotation of 90.00 degrees
"""

    media = _parse_framecrc(stdout, stderr)

    assert media["rotation"] == 90
    assert media["width"] == 720
    assert media["height"] == 1280
