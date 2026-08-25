from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest

from app.schemas import SubtitleRenderOptionsV2
from app.services.media_probe import probe_media
from app.services.subtitle_render import render_precision_video


def test_precision_render_concatenates_kept_video_segments(tmp_path: Path) -> None:
    source = tmp_path / "source-cuts.mp4"
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
            "testsrc2=s=320x180:r=12:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    options = SubtitleRenderOptionsV2(
        encoder="software",
        video_segments=[
            {"id": "start", "start_ms": 0, "end_ms": 500},
            {"id": "end", "start_ms": 1_000, "end_ms": 2_000},
        ],
    ).model_dump(mode="json")
    document = {
        "schema_version": 2,
        "language": "vi",
        "timebase": "milliseconds",
        "timing_source": "manual",
        "timing_precision_ms": 1,
        "segments": [],
    }

    result = render_precision_video(
        source,
        document,
        media,
        options,
        tmp_path / "cut-output",
        video_id="c" * 12,
        fonts_dir=Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo",
        timeout_seconds=60,
    )
    output_media = probe_media(tmp_path / "cut-output" / result["output_filename"])

    assert abs(output_media["duration_ms"] - 1_500) <= 150
    assert output_media["has_audio"] is True
    assert result["audio_copied"] is False


@pytest.mark.parametrize(
    (
        "suffix",
        "video_args",
        "audio_args",
        "expected_video_codec",
        "expected_audio_copied",
    ),
    [
        (
            "mp4",
            ["-c:v", "libx264", "-preset", "ultrafast"],
            ["-c:a", "aac"],
            "h264",
            True,
        ),
        (
            "mov",
            ["-c:v", "libx264", "-preset", "ultrafast"],
            ["-c:a", "pcm_s16le"],
            "h264",
            False,
        ),
        (
            "mkv",
            [
                "-c:v",
                "libx265",
                "-preset",
                "ultrafast",
                "-x265-params",
                "log-level=error:pools=1:frame-threads=1",
            ],
            ["-c:a", "aac"],
            "hevc",
            True,
        ),
        (
            "webm",
            ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8"],
            ["-c:a", "libopus"],
            "vp9",
            True,
        ),
    ],
)
def test_precision_render_codec_and_container_matrix(
    tmp_path: Path,
    suffix: str,
    video_args: list[str],
    audio_args: list[str],
    expected_video_codec: str,
    expected_audio_copied: bool,
) -> None:
    source = tmp_path / f"source.{suffix}"
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
            "testsrc2=s=320x180:r=12:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            *video_args,
            *audio_args,
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    assert media["video_codec"] == expected_video_codec
    assert media["has_audio"] is True

    document = {
        "schema_version": 2,
        "language": "vi",
        "timebase": "milliseconds",
        "timing_source": "manual",
        "timing_precision_ms": 1,
        "segments": [
            {
                "id": "matrix-cue",
                "start_ms": 101,
                "end_ms": 801,
                "text": "Kiểm thử codec tiếng Việt",
                "timing_source": "manual",
                "timing_precision_ms": 1,
                "confidence": None,
                "needs_review": False,
                "revision": 0,
            }
        ],
    }
    options = SubtitleRenderOptionsV2(encoder="software").model_dump(mode="json")
    result = render_precision_video(
        source,
        document,
        media,
        options,
        tmp_path / "output",
        video_id=(expected_video_codec[0] * 12),
        fonts_dir=Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo",
        timeout_seconds=60,
    )
    rendered = tmp_path / "output" / result["output_filename"]
    output_media = probe_media(rendered)

    assert output_media["video_codec"] == "h264"
    assert output_media["has_audio"] is True
    assert result["audio_copied"] is expected_audio_copied
    assert rendered.stat().st_size > 0
