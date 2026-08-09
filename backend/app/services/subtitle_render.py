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

PRECISION_RENDERER_VERSION = "2026-08-subtitle-v2.3"
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
) -> str:
    payload = {
        "renderer": PRECISION_RENDERER_VERSION,
        "media": media.get("fingerprint"),
        "audio": media.get("audio_hash"),
        "document": document,
        "options": options,
        "overlay": overlay,
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


def _video_filter_graph(
    subtitle_path: Path,
    fonts_dir: Path,
    media: dict[str, Any],
    options: dict[str, Any],
    overlay: dict[str, Any] | None = None,
) -> tuple[str, str]:
    escaped_subtitle = _escape_filter_path(subtitle_path)
    escaped_fonts = _escape_filter_path(fonts_dir)
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

    if target:
        width, height = target
        fill = str(options.get("bg_fill_type", "blur"))
        color = str(options.get("bg_color", "#000000")).lstrip("#")
        if fill == "blur":
            graph_parts.extend(
                [
                    "[0:v]split=2[background_source][foreground_source]",
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
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
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

    graph_parts.append(f"{current_video}{setpts},{subtitle_filter}[vout]")
    return ";".join(graph_parts), "[vout]"


def _output_duration_ms(media: dict[str, Any], options: dict[str, Any]) -> int:
    start_ms = int(options.get("trim_start_ms", 0))
    end_ms = min(
        int(media["duration_ms"]),
        int(options.get("trim_end_ms") or media["duration_ms"]),
    )
    if end_ms <= start_ms:
        raise SubtitleRenderError("Render trim range is empty")
    speed = Decimal(str(options.get("video_speed", 1)))
    return int(
        (Decimal(end_ms - start_ms) / speed).quantize(
            Decimal(1),
            rounding=ROUND_HALF_UP,
        )
    )


def _audio_arguments(
    media: dict[str, Any],
    options: dict[str, Any],
    output_duration_ms: int,
) -> tuple[list[str], bool]:
    if not media.get("has_audio"):
        return ["-an"], False
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
        "0:a:0?",
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
    cache_key = precision_render_cache_key(document, media, options, overlay)
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
            }
        except (MediaProbeError, OSError):
            final_output.unlink(missing_ok=True)

    trim_start_ms = int(options.get("trim_start_ms", 0))
    trim_end_ms = min(
        int(options.get("trim_end_ms") or media["duration_ms"]),
        int(media["duration_ms"]),
    )
    transformed = transform_project_cues(
        document.get("segments", []),
        trim_start_ms=trim_start_ms,
        trim_end_ms=trim_end_ms,
        video_speed=Decimal(str(options.get("video_speed", 1))),
    )
    if options.get("uppercase"):
        for cue in transformed:
            cue["text"] = str(cue["text"]).upper()
            if cue.get("secondary_text"):
                cue["secondary_text"] = str(cue["secondary_text"]).upper()
    if not transformed:
        raise SubtitleRenderError("No subtitle cue intersects the render range")
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
            if render_mode == "effects":
                subtitle_path = work_dir / "subtitles.ass"
                effect_cues = [
                    {
                        "start_seconds": int(cue["start_ms"]) / 1000,
                        "end_seconds": int(cue["end_ms"]) / 1000,
                        "text": cue["text"],
                        "secondary_text": cue.get("secondary_text"),
                    }
                    for cue in transformed
                ]
                subtitle_path.write_text(
                    subtitles_to_ass(effect_cues, options),
                    encoding="utf-8",
                )
            else:
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
            )
            audio_args, audio_copied = _audio_arguments(
                media,
                options,
                output_duration_ms,
            )
            input_args: list[str] = []
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
    }
