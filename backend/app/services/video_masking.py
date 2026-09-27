"""Video hardsub and watermark masking service using FFmpeg filtergraphs."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..schemas import SubtitleOcrRegion

logger = logging.getLogger("content_bot.video_masking")


class VideoMaskingError(RuntimeError):
    pass


def generate_hardsub_mask_filter(
    region: SubtitleOcrRegion,
    *,
    mode: str = "blur",
    orig_w: int = 1280,
    orig_h: int = 720,
    blur_power: int = 15,
    patch_color: str = "black@0.75",
) -> str:
    """Generate FFmpeg filtergraph string for masking a subtitle or watermark region."""
    x = max(0, min(orig_w - 2, round(region.x / 100.0 * orig_w)))
    y = max(0, min(orig_h - 2, round(region.y / 100.0 * orig_h)))
    w = max(4, min(orig_w - x, round(region.width / 100.0 * orig_w)))
    h = max(4, min(orig_h - y, round(region.height / 100.0 * orig_h)))

    # Ensure w and h are even for standard codecs
    if w % 2 != 0:
        w -= 1
    if h % 2 != 0:
        h -= 1

    if mode == "patch":
        # Draw solid or semi-transparent rectangular banner over subtitle band
        return f"drawbox=x={x}:y={y}:w={w}:h={h}:color={patch_color}:t=fill"

    # Default: boxblur over cropped sub-rectangle then overlay back
    return (
        f"split[main][crop_in];"
        f"[crop_in]crop={w}:{h}:{x}:{y},boxblur={blur_power}:3[blurred];"
        f"[main][blurred]overlay={x}:{y}"
    )


def apply_hardsub_mask(
    input_video: Path,
    output_video: Path,
    region: SubtitleOcrRegion,
    *,
    mode: str = "blur",
    orig_w: int = 1280,
    orig_h: int = 720,
    blur_power: int = 15,
    patch_color: str = "black@0.75",
    ffmpeg_bin: str | None = None,
) -> Path:
    """Apply hardsub blur or patch mask to video using FFmpeg."""
    if not input_video.exists():
        raise VideoMaskingError(f"Input video not found: {input_video}")

    ffmpeg = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
    filtergraph = generate_hardsub_mask_filter(
        region,
        mode=mode,
        orig_w=orig_w,
        orig_h=orig_h,
        blur_power=blur_power,
        patch_color=patch_color,
    )

    output_video.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_video),
        "-vf",
        filtergraph,
        "-c:a",
        "copy",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        str(output_video),
    ]

    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        stderr_str = exc.stderr.decode("utf-8", errors="replace")
        logger.error("FFmpeg masking failed: %s", stderr_str)
        raise VideoMaskingError(f"Lỗi che dải phụ đề bằng FFmpeg: {stderr_str[-300:]}") from exc

    return output_video
