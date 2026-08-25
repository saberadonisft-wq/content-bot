from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import imageio_ffmpeg

from .media_probe import MediaProbeError, probe_media
from .subtitle_timing import transform_project_cues, validate_cues
from .subtitles import (
    SUBTITLE_DESIGN_HEIGHT,
    css_font_size_to_ass,
    hex_to_ass_color,
    subtitles_to_ass,
    subtitles_to_srt,
)

PRECISION_RENDERER_VERSION = "2026-08-subtitle-v3.5-video-cuts"
SRT_PLAYRES_X = 384
SRT_PLAYRES_Y = 288
RenderProgress = Callable[[int, str, str], None]
EncoderName = Literal["h264_nvenc", "h264_qsv", "libx264"]
_CAPABILITY_LOCK = threading.Lock()
_ENCODER_CAPABILITIES: dict[str, bool] | None = None


class SubtitleRenderError(RuntimeError):
    pass


class SubtitleRenderCanceled(SubtitleRenderError):
    pass


def _check_canceled(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise SubtitleRenderCanceled("Render was canceled")


def _emit(
    callback: RenderProgress | None,
    progress: int,
    phase: str,
    message: str,
) -> None:
    if callback:
        callback(max(0, min(100, progress)), phase, message)


def precision_render_cache_key(
    document: dict[str, Any],
    media: dict[str, Any],
    options: dict[str, Any],
    overlay: dict[str, Any] | None = None,
    masks: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "renderer": PRECISION_RENDERER_VERSION,
        "media": media.get("fingerprint"),
        "audio": media.get("audio_hash"),
        "document": document,
        "options": options,
        "overlay": overlay,
        "masks": masks or [],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encoder_smoke_test(ffmpeg_exe: str, encoder: EncoderName) -> bool:
    try:
        result = subprocess.run(
            [
                ffmpeg_exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=s=320x180:r=30:d=0.1",
                "-frames:v",
                "2",
                "-c:v",
                encoder,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def detect_encoder_capabilities(*, refresh: bool = False) -> dict[str, bool]:
    global _ENCODER_CAPABILITIES
    with _CAPABILITY_LOCK:
        if _ENCODER_CAPABILITIES is not None and not refresh:
            return dict(_ENCODER_CAPABILITIES)
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        _ENCODER_CAPABILITIES = {
            encoder: _encoder_smoke_test(ffmpeg_exe, encoder)
            for encoder in ("h264_nvenc", "h264_qsv", "libx264")
        }
        return dict(_ENCODER_CAPABILITIES)


def encoder_candidates(
    requested: str, capabilities: dict[str, bool]
) -> list[EncoderName]:
    preference: list[EncoderName]
    if requested == "nvenc":
        preference = ["h264_nvenc", "h264_qsv", "libx264"]
    elif requested == "qsv":
        preference = ["h264_qsv", "h264_nvenc", "libx264"]
    elif requested == "software":
        preference = ["libx264"]
    else:
        preference = ["h264_nvenc", "h264_qsv", "libx264"]
    candidates = [encoder for encoder in preference if capabilities.get(encoder)]
    if "libx264" not in candidates:
        candidates.append("libx264")
    return candidates


def _encoder_arguments(encoder: EncoderName, profile: str) -> list[str]:
    if encoder == "h264_nvenc":
        preset = {"fast": "p1", "balanced": "p4", "quality": "p6"}[profile]
        quality = {"fast": "25", "balanced": "22", "quality": "19"}[profile]
        return [
            "-c:v",
            encoder,
            "-preset",
            preset,
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            quality,
            "-b:v",
            "0",
        ]
    if encoder == "h264_qsv":
        preset = {"fast": "veryfast", "balanced": "medium", "quality": "slow"}[profile]
        quality = {"fast": "26", "balanced": "23", "quality": "20"}[profile]
        return ["-c:v", encoder, "-preset", preset, "-global_quality", quality]
    preset = {"fast": "veryfast", "balanced": "medium", "quality": "slow"}[profile]
    quality = {"fast": "24", "balanced": "21", "quality": "18"}[profile]
    return ["-c:v", "libx264", "-preset", preset, "-crf", quality]


def _escape_filter_path(path: Path) -> str:
    return (
        str(path.resolve())
        .replace("\\", "/")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )


def _ass_alignment(options: dict[str, Any]) -> tuple[int, int, int, int]:
    horizontal = str(options.get("alignment_type", "center"))
    position = str(options.get("position", "custom"))
    pos_x = max(0.0, min(100.0, float(options.get("pos_x", 50))))
    pos_y = max(0.0, min(100.0, float(options.get("pos_y", 78))))
    if position == "top":
        vertical = "top"
        pos_y = 10
    elif position == "middle":
        vertical = "middle"
        pos_y = 50
    elif position == "bottom":
        vertical = "bottom"
        pos_y = 90
    else:
        # SRT is converted by libavcodec to a 384x288 ASS canvas. Top/bottom
        # alignment lets MarginV place a custom vertical anchor; compensate by
        # half a design line so pos_y remains the visual center used in preview.
        vertical = "top" if pos_y <= 50 else "bottom"
    alignment = {
        "top": {"left": 7, "center": 8, "right": 9},
        "middle": {"left": 4, "center": 5, "right": 6},
        "bottom": {"left": 1, "center": 2, "right": 3},
    }[vertical][horizontal]
    scaled_font_size = float(options.get("font_size", 38)) * (
        SRT_PLAYRES_Y / SUBTITLE_DESIGN_HEIGHT
    )
    half_line = scaled_font_size * float(options.get("line_spacing", 1.2)) / 2
    if vertical == "middle":
        margin_v = 0
    else:
        edge_percent = pos_y if vertical == "top" else 100 - pos_y
        margin_v = max(0, round(edge_percent / 100 * SRT_PLAYRES_Y - half_line))

    minimum_margin = 4
    maximum_margin = SRT_PLAYRES_X - minimum_margin
    anchor_x = pos_x / 100 * SRT_PLAYRES_X
    if horizontal == "left":
        margin_l = round(anchor_x)
        margin_r = minimum_margin
    elif horizontal == "right":
        margin_l = minimum_margin
        margin_r = round(SRT_PLAYRES_X - anchor_x)
    elif anchor_x <= SRT_PLAYRES_X / 2:
        margin_l = minimum_margin
        margin_r = round(SRT_PLAYRES_X + minimum_margin - 2 * anchor_x)
    else:
        margin_l = round(2 * anchor_x - SRT_PLAYRES_X + minimum_margin)
        margin_r = minimum_margin
    margin_l = max(minimum_margin, min(maximum_margin, margin_l))
    margin_r = max(minimum_margin, min(maximum_margin, margin_r))
    return alignment, margin_v, margin_l, margin_r


def _force_style(options: dict[str, Any]) -> str:
    alignment, margin_v, margin_l, margin_r = _ass_alignment(options)
    design_scale = SRT_PLAYRES_Y / SUBTITLE_DESIGN_HEIGHT
    font_name = re.sub(r"[^A-Za-z0-9 ._-]", "", str(options.get("font_name", "Arimo")))
    primary = hex_to_ass_color(str(options.get("font_color", "#FFFFFF")))
    outline = hex_to_ass_color(str(options.get("outline_color", "#000000")))
    background = hex_to_ass_color(
        str(options.get("bg_color", "#000000")),
        opacity=float(options.get("bg_opacity", 0.75)),
    )
    shadow = hex_to_ass_color(str(options.get("shadow_color", "#000000")))
    back_color = background if options.get("bg_enabled") else shadow
    fields = {
        "FontName": font_name or "Arimo",
        "FontSize": css_font_size_to_ass(
            float(options.get("font_size", 38)), SRT_PLAYRES_Y
        ),
        "PrimaryColour": primary,
        "OutlineColour": outline,
        "BackColour": back_color,
        "Bold": -1 if options.get("bold") else 0,
        "Italic": -1 if options.get("italic") else 0,
        "Underline": -1 if options.get("underline") else 0,
        "StrikeOut": -1 if options.get("strikethrough") else 0,
        "BorderStyle": 3 if options.get("bg_enabled") else 1,
        "Outline": round(float(options.get("outline_width", 2)) * design_scale, 2),
        "Shadow": round(float(options.get("shadow_width", 1)) * design_scale, 2),
        "Spacing": round(float(options.get("spacing", 0)) * design_scale, 2),
        "Alignment": alignment,
        "MarginL": margin_l,
        "MarginR": margin_r,
        "MarginV": margin_v,
    }
    return ",".join(f"{key}={value}" for key, value in fields.items())


def _target_dimensions(
    media: dict[str, Any], aspect_ratio: str
) -> tuple[int, int] | None:
    width = int(media["width"])
    height = int(media["height"])
    long_edge = max(width, height)
    if aspect_ratio == "original":
        return None
    large = long_edge > 1280
    if aspect_ratio == "9:16":
        return (1080, 1920) if large else (720, 1280)
    if aspect_ratio == "1:1":
        edge = 1080 if large else 720
        return edge, edge
    return (1920, 1080) if large else (1280, 720)


def subtitle_play_resolution(
    media: dict[str, Any], options: dict[str, Any]
) -> tuple[int, int]:
    """Return a 720p-relative ASS canvas matching the final display aspect."""
    target = _target_dimensions(media, str(options.get("aspect_ratio", "original")))
    output_width = target[0] if target else int(media["width"])
    output_height = target[1] if target else int(media["height"])
    play_res_y = SUBTITLE_DESIGN_HEIGHT
    play_res_x = max(1, round(play_res_y * output_width / max(1, output_height)))
    return play_res_x, play_res_y


def _mask_pixel_geometry(
    mask: dict[str, Any],
    output_width: int,
    output_height: int,
) -> tuple[int, int, int, int]:
    """Convert percentage geometry to an even-aligned source-frame ROI."""
    maximum_x = max(2, output_width - output_width % 2)
    maximum_y = max(2, output_height - output_height % 2)
    left = round(output_width * float(mask.get("x", 20)) / 100)
    top = round(output_height * float(mask.get("y", 72)) / 100)
    right = round(
        output_width
        * (float(mask.get("x", 20)) + float(mask.get("width", 60)))
        / 100
    )
    bottom = round(
        output_height
        * (float(mask.get("y", 72)) + float(mask.get("height", 12)))
        / 100
    )
    left = max(0, min(maximum_x - 2, left - left % 2))
    top = max(0, min(maximum_y - 2, top - top % 2))
    right = max(left + 2, min(maximum_x, right + right % 2))
    bottom = max(top + 2, min(maximum_y, bottom + bottom % 2))
    return left, top, right - left, bottom - top


def _expanded_mask_roi(
    geometry: tuple[int, int, int, int],
    output_width: int,
    output_height: int,
    padding: int,
) -> tuple[int, int, int, int]:
    x, y, width, height = geometry
    maximum_x = max(2, output_width - output_width % 2)
    maximum_y = max(2, output_height - output_height % 2)
    left = max(0, x - padding)
    top = max(0, y - padding)
    right = min(maximum_x, x + width + padding)
    bottom = min(maximum_y, y + height + padding)
    left -= left % 2
    top -= top % 2
    right = min(maximum_x, right + right % 2)
    bottom = min(maximum_y, bottom + bottom % 2)
    return left, top, max(2, right - left), max(2, bottom - top)


def _mask_shape_expression(
    mask: dict[str, Any],
    x: int,
    y: int,
    width: int,
    height: int,
) -> str:
    shape = str(mask.get("shape", "rectangle"))
    right = x + width - 1
    bottom = y + height - 1
    if shape == "ellipse":
        radius_x = max(0.5, width / 2)
        radius_y = max(0.5, height / 2)
        center_x = x + width / 2
        center_y = y + height / 2
        return (
            f"lte(pow((X-{center_x:.3f})/{radius_x:.3f},2)+"
            f"pow((Y-{center_y:.3f})/{radius_y:.3f},2),1)"
        )
    if shape == "rounded":
        radius_percent = max(0.0, min(50.0, float(mask.get("corner_radius", 14))))
        radius = max(0.5, min(width, height) * radius_percent / 100)
        center_x = x + width / 2
        center_y = y + height / 2
        inner_x = max(0.0, width / 2 - radius)
        inner_y = max(0.0, height / 2 - radius)
        return (
            "lte(hypot("
            f"max(abs(X-{center_x:.3f})-{inner_x:.3f},0),"
            f"max(abs(Y-{center_y:.3f})-{inner_y:.3f},0)),"
            f"{radius:.3f})"
        )
    return f"between(X,{x},{right})*between(Y,{y},{bottom})"


def _append_mask_filters(
    graph_parts: list[str],
    current_video: str,
    masks: list[dict[str, Any]],
    output_width: int,
    output_height: int,
) -> str:
    for index, mask in enumerate(masks):
        prefix = f"subtitle_mask_{index}"
        strength = max(1.0, min(40.0, float(mask.get("strength", 14))))
        effect = str(mask.get("effect", "blur"))
        strength_multiplier = 1.5 if effect == "blur" else 1.0
        scaled_strength = max(
            0.25,
            strength * strength_multiplier * output_height / 720,
        )
        opacity = (
            1.0
            if effect in {"blur", "pixelate"}
            else max(0.05, min(1.0, float(mask.get("opacity", 1))))
        )
        feather = max(0.0, min(20.0, float(mask.get("feather", 2))))
        scaled_feather = feather * output_height / 720
        geometry = _mask_pixel_geometry(mask, output_width, output_height)
        mask_x, mask_y, mask_width, mask_height = geometry
        shape = str(mask.get("shape", "rectangle"))
        color = (
            str(mask.get("color", "#000000")).lstrip("#")
            if effect == "solid"
            else "000000"
        )

        # Rectangular fills need no frame split, crop, alpha plane or overlay.
        if effect in {"solid", "darken"} and shape in {"rectangle", "band"} and feather == 0:
            graph_parts.append(
                f"{current_video}drawbox=x={mask_x}:y={mask_y}:"
                f"w={mask_width}:h={mask_height}:"
                f"color=0x{color}@{opacity:.4f}:t=fill[{prefix}_output]"
            )
            current_video = f"[{prefix}_output]"
            continue

        blur_padding = round(scaled_strength * 2) if effect == "blur" else 0
        feather_padding = round(scaled_feather * 3) if scaled_feather > 0 else 0
        roi_x, roi_y, roi_width, roi_height = _expanded_mask_roi(
            geometry,
            output_width,
            output_height,
            blur_padding + feather_padding,
        )
        local_x = mask_x - roi_x
        local_y = mask_y - roi_y
        graph_parts.append(
            # Pin the shared source to a color format before the gray alpha
            # branch, while cropping expensive filters to the selected ROI.
            f"{current_video}format=yuv420p,split=2"
            f"[{prefix}_base][{prefix}_roi_source]"
        )
        needs_alpha = shape not in {"rectangle", "band"} or scaled_feather > 0
        split_count = 2 if needs_alpha else 1
        crop_filter = (
            f"[{prefix}_roi_source]crop={roi_width}:{roi_height}:{roi_x}:{roi_y}"
        )
        if split_count == 2:
            graph_parts.append(
                f"{crop_filter},split=2"
                f"[{prefix}_effect_source][{prefix}_alpha_source]"
            )
        else:
            graph_parts.append(f"{crop_filter}[{prefix}_effect_source]")

        if effect == "pixelate":
            block_size = max(2, round(scaled_strength))
            small_width = max(1, roi_width // block_size)
            small_height = max(1, roi_height // block_size)
            graph_parts.append(
                f"[{prefix}_effect_source]scale={small_width}:{small_height}:flags=neighbor,"
                f"scale={roi_width}:{roi_height}:flags=neighbor[{prefix}_effect]"
            )
        elif effect in {"solid", "darken"}:
            graph_parts.append(
                f"[{prefix}_effect_source]drawbox=x=0:y=0:w=iw:h=ih:"
                f"color=0x{color}:t=fill[{prefix}_effect]"
            )
        else:
            graph_parts.append(
                f"[{prefix}_effect_source]gblur=sigma={scaled_strength:.3f}"
                f"[{prefix}_effect]"
            )

        if not needs_alpha:
            patch_label = f"[{prefix}_effect]"
            if roi_width != mask_width or roi_height != mask_height:
                graph_parts.append(
                    f"{patch_label}crop={mask_width}:{mask_height}:"
                    f"{local_x}:{local_y}[{prefix}_patch]"
                )
                patch_label = f"[{prefix}_patch]"
            graph_parts.append(
                f"[{prefix}_base]{patch_label}overlay={mask_x}:{mask_y}:"
                f"eof_action=pass:shortest=0:format=auto[{prefix}_output]"
            )
            current_video = f"[{prefix}_output]"
            continue

        shape_expression = _mask_shape_expression(
            mask,
            local_x,
            local_y,
            mask_width,
            mask_height,
        )
        alpha_filter = (
            f"[{prefix}_alpha_source]format=gray,"
            f"geq=lum='255*{opacity:.4f}*({shape_expression})'"
        )
        if scaled_feather > 0:
            alpha_filter += f",gblur=sigma={scaled_feather:.3f}"
        graph_parts.append(f"{alpha_filter}[{prefix}_alpha]")
        graph_parts.append(
            f"[{prefix}_effect]format=rgba[{prefix}_rgba]"
        )
        graph_parts.append(
            f"[{prefix}_rgba][{prefix}_alpha]alphamerge[{prefix}_masked]"
        )
        graph_parts.append(
            f"[{prefix}_base][{prefix}_masked]overlay={roi_x}:{roi_y}:"
            f"eof_action=pass:shortest=0:format=auto[{prefix}_output]"
        )
        current_video = f"[{prefix}_output]"
    return current_video


def _video_filter_graph(
    subtitle_path: Path | None,
    fonts_dir: Path,
    media: dict[str, Any],
    options: dict[str, Any],
    overlay: dict[str, Any] | None = None,
    masks: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    escaped_fonts = _escape_filter_path(fonts_dir)
    subtitle_filter: str | None = None
    if subtitle_path is not None:
        escaped_subtitle = _escape_filter_path(subtitle_path)
        subtitle_filter = (
            f"subtitles=filename='{escaped_subtitle}':fontsdir='{escaped_fonts}'"
        )
        if subtitle_path.suffix.lower() != ".ass":
            subtitle_filter += f":charenc=UTF-8:force_style='{_force_style(options)}'"
    speed = Decimal(str(options.get("video_speed", 1)))
    setpts = "setpts=PTS-STARTPTS" if speed == 1 else f"setpts=(PTS-STARTPTS)/{speed}"
    target = _target_dimensions(media, str(options.get("aspect_ratio", "original")))
    output_width = target[0] if target else int(media["width"])
    graph_parts: list[str] = []
    current_video = "[0:v]"
    video_segments: list[tuple[int, int]] = []
    cuts_active = False
    if options.get("video_segments"):
        video_segments, cuts_active = _effective_video_segments(media, options)
    if cuts_active:
        video_labels: list[str] = []
        for index, (start_ms, end_ms) in enumerate(video_segments):
            label = f"cut_video_{index}"
            graph_parts.append(
                f"[0:v]trim=start={start_ms / 1000:.3f}:end={end_ms / 1000:.3f},"
                f"setpts=PTS-STARTPTS[{label}]"
            )
            video_labels.append(f"[{label}]")
        if len(video_labels) == 1:
            current_video = video_labels[0]
        else:
            graph_parts.append(
                f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[cut_video]"
            )
            current_video = "[cut_video]"
        if media.get("has_audio"):
            audio_labels: list[str] = []
            for index, (start_ms, end_ms) in enumerate(video_segments):
                label = f"cut_audio_{index}"
                graph_parts.append(
                    f"[0:a]atrim=start={start_ms / 1000:.3f}:end={end_ms / 1000:.3f},"
                    f"asetpts=PTS-STARTPTS[{label}]"
                )
                audio_labels.append(f"[{label}]")
            if len(audio_labels) == 1:
                graph_parts.append(f"{audio_labels[0]}anull[acut]")
            else:
                graph_parts.append(
                    f"{''.join(audio_labels)}concat=n={len(audio_labels)}:v=0:a=1[acut]"
                )
            audio_filters = ["asetpts=PTS-STARTPTS"]
            volume = float(options.get("volume", 1))
            fade_in_ms = int(options.get("fade_in_ms", 0))
            fade_out_ms = int(options.get("fade_out_ms", 0))
            if speed != 1:
                audio_filters.append(f"atempo={speed}")
            if volume != 1:
                audio_filters.append(f"volume={volume:.4f}")
            if fade_in_ms > 0:
                audio_filters.append(f"afade=t=in:st=0:d={fade_in_ms / 1000:.3f}")
            if fade_out_ms > 0:
                output_duration_ms = _output_duration_ms(media, options)
                fade_start = max(0, output_duration_ms - fade_out_ms) / 1000
                audio_filters.append(
                    f"afade=t=out:st={fade_start:.3f}:d={fade_out_ms / 1000:.3f}"
                )
            graph_parts.append(f"[acut]{','.join(audio_filters)}[aout]")

    # Mask coordinates are percentages of the source frame shown in the editor.
    # Apply them before any aspect-ratio padding/cropping so preview and export
    # stay aligned even when the final canvas changes shape.
    if masks:
        current_video = _append_mask_filters(
            graph_parts,
            current_video,
            masks,
            int(media["width"]),
            int(media["height"]),
        )

    if target:
        width, height = target
        fill = str(options.get("bg_fill_type", "blur"))
        color = str(options.get("bg_color", "#000000")).lstrip("#")
        if fill == "blur":
            graph_parts.extend(
                [
                    f"{current_video}split=2[background_source][foreground_source]",
                    (
                        f"[background_source]scale={width}:{height}:"
                        "force_original_aspect_ratio=increase,"
                        f"crop={width}:{height},gblur=sigma=28[background]"
                    ),
                    f"[foreground_source]scale={width}:{height}:force_original_aspect_ratio=decrease[foreground]",
                    "[background][foreground]overlay=(W-w)/2:(H-h)/2[composed]",
                ]
            )
        else:
            pad_color = "black" if fill == "black" else f"0x{color}"
            graph_parts.append(
                f"{current_video}scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={pad_color}[composed]"
            )
        current_video = "[composed]"

    if overlay:
        overlay_width_percent = max(4.0, min(90.0, float(overlay["width"])))
        overlay_x = max(0.0, min(100.0, float(overlay["x"]))) / 100
        overlay_y = max(0.0, min(100.0, float(overlay["y"]))) / 100
        overlay_width = max(1, round(output_width * overlay_width_percent / 100))
        graph_parts.append(
            f"[1:v]format=rgba,scale={overlay_width}:-1:flags=lanczos,setsar=1[overlay_asset]"
        )
        graph_parts.append(
            f"{current_video}[overlay_asset]overlay="
            f"x='W*{overlay_x:.6f}-w/2':y='H*{overlay_y:.6f}-h/2':"
            "eof_action=repeat:shortest=0:format=auto[with_overlay]"
        )
        current_video = "[with_overlay]"

    video_chain = f"{current_video}{setpts}"
    if subtitle_filter:
        video_chain += f",{subtitle_filter}"
    graph_parts.append(f"{video_chain}[vout]")
    return ";".join(graph_parts), "[vout]"


def _output_duration_ms(media: dict[str, Any], options: dict[str, Any]) -> int:
    segments, _ = _effective_video_segments(media, options)
    source_duration_ms = sum(end_ms - start_ms for start_ms, end_ms in segments)
    speed = Decimal(str(options.get("video_speed", 1)))
    return int(
        (Decimal(source_duration_ms) / speed).quantize(
            Decimal(1),
            rounding=ROUND_HALF_UP,
        )
    )


def _effective_video_segments(
    media: dict[str, Any],
    options: dict[str, Any],
) -> tuple[list[tuple[int, int]], bool]:
    duration_ms = int(media["duration_ms"])
    trim_start_ms = max(0, int(options.get("trim_start_ms", 0)))
    trim_end_ms = min(
        duration_ms,
        int(options.get("trim_end_ms") or duration_ms),
    )
    raw_segments = options.get("video_segments") or []
    segments: list[tuple[int, int]] = []
    if raw_segments:
        for segment in raw_segments:
            start_ms = max(trim_start_ms, int(segment["start_ms"]))
            end_ms = min(trim_end_ms, duration_ms, int(segment["end_ms"]))
            if end_ms <= start_ms:
                continue
            if segments and start_ms < segments[-1][1]:
                raise SubtitleRenderError("Video segments overlap")
            if segments and start_ms == segments[-1][1]:
                segments[-1] = (segments[-1][0], end_ms)
            else:
                segments.append((start_ms, end_ms))
    elif trim_end_ms > trim_start_ms:
        segments = [(trim_start_ms, trim_end_ms)]
    if not segments:
        raise SubtitleRenderError("Render video segments are empty")
    cuts_active = segments != [(trim_start_ms, trim_end_ms)]
    return segments, cuts_active


def _transform_cues_for_video_segments(
    cues: list[dict[str, Any]],
    segments: list[tuple[int, int]],
    video_speed: Decimal,
) -> list[dict[str, Any]]:
    transformed: list[dict[str, Any]] = []
    source_offset_ms = 0
    id_counts: dict[str, int] = {}
    for start_ms, end_ms in segments:
        segment_cues = transform_project_cues(
            cues,
            trim_start_ms=start_ms,
            trim_end_ms=end_ms,
            video_speed=video_speed,
        )
        output_offset_ms = int(
            (Decimal(source_offset_ms) / video_speed).quantize(
                Decimal(1),
                rounding=ROUND_HALF_UP,
            )
        )
        for cue in segment_cues:
            mapped = dict(cue)
            mapped["start_ms"] += output_offset_ms
            mapped["end_ms"] += output_offset_ms
            if mapped.get("speech_start_ms") is not None:
                mapped["speech_start_ms"] += output_offset_ms
            if mapped.get("speech_end_ms") is not None:
                mapped["speech_end_ms"] += output_offset_ms
            for word in mapped.get("words") or []:
                word["start_ms"] += output_offset_ms
                word["end_ms"] += output_offset_ms
            cue_id = str(mapped.get("id", "cue"))
            id_counts[cue_id] = id_counts.get(cue_id, 0) + 1
            if id_counts[cue_id] > 1:
                mapped["id"] = f"{cue_id}-part-{id_counts[cue_id]}"
            transformed.append(mapped)
        source_offset_ms += end_ms - start_ms
    return transformed


def _audio_arguments(
    media: dict[str, Any],
    options: dict[str, Any],
    output_duration_ms: int,
    *,
    audio_source: str = "0:a:0?",
) -> tuple[list[str], bool]:
    if not media.get("has_audio"):
        return ["-an"], False
    if audio_source != "0:a:0?":
        return ["-map", audio_source, "-c:a", "aac", "-b:a", "160k"], False
    speed = Decimal(str(options.get("video_speed", 1)))
    volume = float(options.get("volume", 1))
    fade_in_ms = int(options.get("fade_in_ms", 0))
    fade_out_ms = int(options.get("fade_out_ms", 0))
    audio_codec = str(media.get("audio_codec") or "").lower()
    mp4_copy_compatible = audio_codec in {
        "aac",
        "ac3",
        "alac",
        "eac3",
        "mp3",
        "opus",
    }
    audio_unchanged = (
        speed == 1
        and volume == 1
        and fade_in_ms == 0
        and fade_out_ms == 0
        and mp4_copy_compatible
    )
    if audio_unchanged:
        return ["-map", "0:a:0?", "-c:a", "copy"], True

    filters = ["asetpts=PTS-STARTPTS"]
    if speed != 1:
        filters.append(f"atempo={speed}")
    if volume != 1:
        filters.append(f"volume={volume:.4f}")
    if fade_in_ms > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in_ms / 1000:.3f}")
    if fade_out_ms > 0:
        fade_start = max(0, output_duration_ms - fade_out_ms) / 1000
        filters.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out_ms / 1000:.3f}")
    return [
        "-map",
        audio_source,
        "-af",
        ",".join(filters),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
    ], False


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _run_ffmpeg(
    command: list[str],
    error_log: Path,
    output_duration_ms: int,
    *,
    cancel_event: threading.Event | None,
    progress: RenderProgress | None,
    timeout_seconds: int,
) -> None:
    with error_log.open("w", encoding="utf-8", errors="replace") as stderr_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + timeout_seconds
        lines: queue.Queue[str | None] = queue.Queue()

        def read_progress() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(
            target=read_progress,
            name="subtitle-render-progress",
            daemon=True,
        )
        reader.start()
        try:
            while True:
                _check_canceled(cancel_event)
                if time.monotonic() >= deadline:
                    raise SubtitleRenderError("FFmpeg render timed out")
                try:
                    raw_line = lines.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if raw_line is None:
                    break
                key, separator, value = raw_line.strip().partition("=")
                if not separator:
                    continue
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        rendered_ms = int(value) // 1000
                    except ValueError:
                        continue
                    percent = min(95, 5 + round(rendered_ms / output_duration_ms * 90))
                    _emit(progress, percent, "encoding", "Đang mã hóa video")
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise SubtitleRenderError("FFmpeg did not exit after encoding") from exc
        finally:
            _stop_process(process)
            reader.join(timeout=2)
    if return_code != 0:
        detail = error_log.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise SubtitleRenderError(detail or f"FFmpeg exited with code {return_code}")


def render_precision_video(
    video_path: Path,
    document: dict[str, Any],
    media: dict[str, Any],
    options: dict[str, Any],
    output_dir: Path,
    *,
    video_id: str,
    fonts_dir: Path,
    overlay_path: Path | None = None,
    overlay: dict[str, Any] | None = None,
    masks: list[dict[str, Any]] | None = None,
    cancel_event: threading.Event | None = None,
    progress: RenderProgress | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    _check_canceled(cancel_event)
    render_mode = str(options.get("render_mode", "precision"))
    if render_mode not in {"precision", "effects"}:
        raise SubtitleRenderError("Unknown subtitle render mode")
    if (overlay_path is None) != (overlay is None):
        raise SubtitleRenderError("Overlay path and layout must be provided together")
    if overlay_path is not None and not overlay_path.is_file():
        raise SubtitleRenderError("Overlay image does not exist")
    cache_key = precision_render_cache_key(document, media, options, overlay, masks)
    output_duration_ms = _output_duration_ms(media, options)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_filename = f"subtitled_{video_id}_{cache_key[:12]}.mp4"
    final_output = output_dir / output_filename
    if final_output.exists():
        try:
            probe_media(final_output, timeout_seconds=60)
            _emit(progress, 100, "complete", "Dùng video đã render trong cache")
            return {
                "video_id": video_id,
                "output_filename": output_filename,
                "subtitled_video_url": f"/api/v1/subtitles/renders/{output_filename}",
                "encoder": "cached",
                "cache_hit": True,
                "audio_copied": None,
                "output_size_bytes": final_output.stat().st_size,
                "duration_ms": output_duration_ms,
                "fallback_reasons": [],
                "render_mode": render_mode,
                "timing_precision_ms": 1 if render_mode == "precision" else 10,
                "overlay_applied": overlay is not None,
                "mask_count": len(masks or []),
            }
        except (MediaProbeError, OSError):
            final_output.unlink(missing_ok=True)

    trim_start_ms = int(options.get("trim_start_ms", 0))
    trim_end_ms = min(
        int(options.get("trim_end_ms") or media["duration_ms"]),
        int(media["duration_ms"]),
    )
    video_segments, cuts_active = _effective_video_segments(media, options)
    transformed = _transform_cues_for_video_segments(
        document.get("segments", []),
        video_segments,
        Decimal(str(options.get("video_speed", 1))),
    )
    if options.get("uppercase"):
        for cue in transformed:
            cue["text"] = str(cue["text"]).upper()
            if cue.get("secondary_text"):
                cue["secondary_text"] = str(cue["secondary_text"]).upper()
    warnings = validate_cues(transformed)
    if render_mode == "effects":
        warnings.append(
            {
                "code": "effects_timing_quantized",
                "cue_id": None,
                "message": "Chế độ hiệu ứng ASS lượng tử hóa timestamp ở độ phân giải 10 ms.",
            }
        )
    invalid = [warning for warning in warnings if warning["code"] == "invalid_range"]
    if invalid:
        raise SubtitleRenderError("Transformed subtitle timeline is invalid")

    part_output = output_dir / f"{output_filename}.part.mp4"
    part_output.unlink(missing_ok=True)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    capabilities = detect_encoder_capabilities()
    candidates = encoder_candidates(str(options.get("encoder", "auto")), capabilities)
    fallback_reasons: list[str] = []
    started_at = time.perf_counter()

    work_root = output_dir.parent / "render-work"
    work_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"{video_id[:8]}-", dir=work_root
        ) as work:
            work_dir = Path(work)
            subtitle_path: Path | None = None
            if transformed and render_mode == "effects":
                subtitle_path = work_dir / "subtitles.ass"
                play_res_x, play_res_y = subtitle_play_resolution(media, options)
                subtitle_path.write_text(
                    subtitles_to_ass(
                        transformed,
                        options,
                        play_res_x=play_res_x,
                        play_res_y=play_res_y,
                    ),
                    encoding="utf-8",
                )
            elif transformed:
                subtitle_path = work_dir / "subtitles.srt"
                subtitle_path.write_text(
                    subtitles_to_srt(transformed), encoding="utf-8"
                )
            filter_graph, video_map = _video_filter_graph(
                subtitle_path,
                fonts_dir,
                media,
                options,
                overlay,
                masks,
            )
            audio_args, audio_copied = _audio_arguments(
                media,
                options,
                output_duration_ms,
                audio_source="[aout]" if cuts_active and media.get("has_audio") else "0:a:0?",
            )
            input_args: list[str] = []
            if not cuts_active:
                if trim_start_ms > 0:
                    input_args.extend(["-ss", f"{trim_start_ms / 1000:.3f}"])
                source_clip_ms = min(trim_end_ms, int(media["duration_ms"])) - trim_start_ms
                input_args.extend(["-t", f"{source_clip_ms / 1000:.3f}"])

            last_error: SubtitleRenderError | None = None
            selected_encoder: EncoderName | None = None
            for encoder_index, encoder in enumerate(candidates):
                _check_canceled(cancel_event)
                if encoder_index:
                    _emit(
                        progress,
                        4,
                        "fallback",
                        f"Thử encoder dự phòng {encoder}",
                    )
                part_output.unlink(missing_ok=True)
                error_log = work_dir / f"ffmpeg-{encoder}.log"
                command = [
                    ffmpeg_exe,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-stats_period",
                    "0.25",
                    *input_args,
                    "-i",
                    str(video_path.resolve()),
                    *(["-i", str(overlay_path.resolve())] if overlay_path else []),
                    "-filter_complex",
                    filter_graph,
                    "-map",
                    video_map,
                    *audio_args,
                    *_encoder_arguments(encoder, str(options.get("profile", "fast"))),
                    "-pix_fmt",
                    "yuv420p",
                    "-fps_mode",
                    "passthrough",
                    "-movflags",
                    "+faststart",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    str(part_output.resolve()),
                ]
                try:
                    _run_ffmpeg(
                        command,
                        error_log,
                        output_duration_ms,
                        cancel_event=cancel_event,
                        progress=progress,
                        timeout_seconds=timeout_seconds,
                    )
                    selected_encoder = encoder
                    last_error = None
                    break
                except SubtitleRenderCanceled:
                    raise
                except SubtitleRenderError as exc:
                    last_error = exc
                    fallback_reasons.append(f"{encoder}: {str(exc)[-500:]}")
            if selected_encoder is None:
                raise last_error or SubtitleRenderError("No video encoder succeeded")

            _emit(progress, 96, "verify", "Đang kiểm tra file output")
            output_media = probe_media(part_output, timeout_seconds=120)
            if abs(int(output_media["duration_ms"]) - output_duration_ms) > 1000:
                raise SubtitleRenderError(
                    "Rendered duration does not match the project timeline"
                )
            part_output.replace(final_output)
    finally:
        part_output.unlink(missing_ok=True)

    elapsed_seconds = time.perf_counter() - started_at
    _emit(progress, 100, "complete", "Render hoàn tất")
    return {
        "video_id": video_id,
        "output_filename": output_filename,
        "subtitled_video_url": f"/api/v1/subtitles/renders/{output_filename}",
        "encoder": selected_encoder,
        "cache_hit": False,
        "audio_copied": audio_copied,
        "output_size_bytes": final_output.stat().st_size,
        "duration_ms": output_duration_ms,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "realtime_factor": round(
            output_duration_ms / 1000 / max(0.001, elapsed_seconds), 3
        ),
        "fallback_reasons": fallback_reasons,
        "warnings": warnings,
        "render_mode": render_mode,
        "timing_precision_ms": 1 if render_mode == "precision" else 10,
        "overlay_applied": overlay is not None,
        "mask_count": len(masks or []),
    }
