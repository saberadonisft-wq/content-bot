from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import imageio_ffmpeg


def parse_timestamp_to_seconds(ts_str: str) -> float:
    ts_str = ts_str.strip().replace(",", ".")
    parts = ts_str.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return float(m) * 60 + float(s)
    else:
        return float(ts_str)


def seconds_to_srt_time(seconds: float) -> str:
    millis = int(round((seconds % 1) * 1000))
    total_secs = int(seconds)
    secs = total_secs % 60
    mins = (total_secs // 60) % 60
    hrs = total_secs // 3600
    if millis >= 1000:
        millis -= 1000
        total_secs += 1
        secs = total_secs % 60
        mins = (total_secs // 60) % 60
        hrs = total_secs // 3600
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def parse_subtitles_text(raw_text: str) -> list[dict[str, Any]]:
    text = raw_text.strip()
    if not text:
        return []

    # 1. Try JSON format
    if text.startswith("[") and text.endswith("]"):
        try:
            data = json.loads(text)
            subtitles = []
            for item in data:
                start_raw = str(item.get("start", item.get("start_time", "00:00")))
                end_raw = str(item.get("end", item.get("end_time", "00:05")))
                content = str(item.get("text", item.get("content", ""))).strip()
                s_sec = parse_timestamp_to_seconds(start_raw)
                e_sec = parse_timestamp_to_seconds(end_raw)
                if content:
                    subtitles.append({
                        "start_time": seconds_to_srt_time(s_sec),
                        "end_time": seconds_to_srt_time(e_sec),
                        "start_seconds": s_sec,
                        "end_seconds": e_sec,
                        "text": content,
                    })
            if subtitles:
                return subtitles
        except Exception:
            pass

    # 2. Try inline brackets [MM:SS - MM:SS] text
    bracket_pattern = re.compile(
        r"\[?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\s*[-–—>]\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\s*\]?:?\s*(.*)"
    )
    subtitles = []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]

        # Match inline bracket or timestamp: "[00:00 - 00:03] Hello" or "00:00 - 00:03: Hello"
        match = bracket_pattern.match(line)
        if match:
            s_raw, e_raw, content = match.groups()
            content = content.strip()

            # If content was empty on the same line, look at next line
            if not content and i + 1 < len(lines) and not bracket_pattern.match(lines[i + 1]):
                i += 1
                content = lines[i]

            # Remove leading bullets, colons, or dashes if present
            content = re.sub(r"^[:\-\|\s]+", "", content).strip()

            s_sec = parse_timestamp_to_seconds(s_raw)
            e_sec = parse_timestamp_to_seconds(e_raw)
            if content:
                subtitles.append({
                    "start_time": seconds_to_srt_time(s_sec),
                    "end_time": seconds_to_srt_time(e_sec),
                    "start_seconds": s_sec,
                    "end_seconds": e_sec,
                    "text": content,
                })
            i += 1
            continue

        # 3. Match SRT format: "00:00:00,000 --> 00:00:03,500"
        if "-->" in line:
            parts = line.split("-->")
            if len(parts) == 2:
                s_sec = parse_timestamp_to_seconds(parts[0])
                e_sec = parse_timestamp_to_seconds(parts[1])
                i += 1
                text_lines = []
                while i < len(lines) and not lines[i].isdigit() and "-->" not in lines[i]:
                    text_lines.append(lines[i])
                    i += 1
                content = " ".join(text_lines).strip()
                if content:
                    subtitles.append({
                        "start_time": seconds_to_srt_time(s_sec),
                        "end_time": seconds_to_srt_time(e_sec),
                        "start_seconds": s_sec,
                        "end_seconds": e_sec,
                        "text": content,
                    })
                continue

        i += 1

    return subtitles


def subtitles_to_srt(subtitles: list[dict[str, Any]]) -> str:
    blocks = []
    for idx, sub in enumerate(subtitles, 1):
        s_time = sub["start_time"]
        e_time = sub["end_time"]
        text = sub["text"]
        blocks.append(f"{idx}\n{s_time} --> {e_time}\n{text}\n")
    return "\n".join(blocks)


def hex_to_ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """Convert #RRGGBB or #RGB to ASS format &HAABBGGRR where AA is transparency (00=opaque, FF=transparent)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    alpha = max(0, min(255, int(round((1.0 - opacity) * 255))))
    if len(hex_color) == 6:
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        return f"&H{alpha:02X}{b}{g}{r}"
    return f"&H{alpha:02X}FFFFFF"


def burn_subtitles_to_video(
    video_path: Path,
    subtitles: list[dict[str, Any]],
    options: dict[str, Any],
    output_path: Path,
) -> Path:
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Uppercase transformation if requested
    processed_subtitles = []
    use_uppercase = options.get("uppercase", False)
    for sub in subtitles:
        item = dict(sub)
        if use_uppercase:
            item["text"] = item["text"].upper()
        processed_subtitles.append(item)

    srt_content = subtitles_to_srt(processed_subtitles)
    
    # Save temp srt file in the same directory as output_path to avoid path escaping issues in FFmpeg filter
    temp_srt_path = output_path.parent / f"temp_{uuid.uuid4().hex[:8]}.srt"
    temp_srt_path.write_text(srt_content, encoding="utf-8")

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Configure style options for FFmpeg subtitles filter
    font_name = options.get("font_name", "Arial")
    font_size = options.get("font_size", 24)
    font_color = hex_to_ass_color(options.get("font_color", "#FFFFFF"))
    bold = 1 if options.get("bold", False) else 0
    italic = 1 if options.get("italic", False) else 0
    outline_color = hex_to_ass_color(options.get("outline_color", "#000000"))
    outline_width = options.get("outline_width", 2)
    shadow_color = hex_to_ass_color(options.get("shadow_color", "#000000"))
    shadow_width = options.get("shadow_width", 0)
    spacing = options.get("spacing", 0)
    bg_enabled = options.get("bg_enabled", False)
    bg_opacity = options.get("bg_opacity", 0.75)
    bg_color = hex_to_ass_color(options.get("bg_color", "#000000"), opacity=bg_opacity)
    position = options.get("position", "bottom")

    border_style = 3 if bg_enabled else 1

    alignment = 2  # bottom center
    margin_v = 20
    margin_l = 20

    if position == "middle":
        alignment = 10  # middle center
    elif position == "top":
        alignment = 6  # top center
    elif position == "custom":
        pos_y = options.get("pos_y", 85.0)
        pos_x = options.get("pos_x", 50.0)
        alignment = 7  # Top-left origin
        # Assuming reference resolution 1280x720 in ASS
        margin_l = max(0, int((pos_x / 100.0) * 1280))
        margin_v = max(0, int((pos_y / 100.0) * 720))

    force_style = (
        f"Fontname='{font_name}',"
        f"FontSize={font_size},"
        f"PrimaryColour={font_color},"
        f"Bold={bold},"
        f"Italic={italic},"
        f"OutlineColour={outline_color},"
        f"Outline={outline_width},"
        f"ShadowColour={shadow_color},"
        f"Shadow={shadow_width},"
        f"Spacing={spacing},"
        f"BorderStyle={border_style},"
        f"BackColour={bg_color},"
        f"Alignment={alignment},"
        f"MarginL={margin_l},"
        f"MarginV={margin_v}"
    )

    # Video editing options
    video_speed = options.get("video_speed", 1.0)
    volume = options.get("volume", 1.0)
    fade_in = options.get("fade_in", 0.0)
    fade_out = options.get("fade_out", 0.0)
    aspect_ratio = options.get("aspect_ratio", "16:9")
    bg_fill_type = options.get("bg_fill_type", "blur")
    trim_start = options.get("trim_start", 0.0)
    trim_end = options.get("trim_end", None)

    # Convert Windows path for FFmpeg subtitles filter: C:\path\sub.srt -> C\:/path/sub.srt
    escaped_srt = str(temp_srt_path.resolve()).replace("\\", "/").replace(":", "\\:")
    sub_filter = f"subtitles='{escaped_srt}':force_style='{force_style}'"

    # Input trimming options
    input_args = []
    if trim_start > 0:
        input_args.extend(["-ss", str(trim_start)])
    if trim_end is not None and trim_end > trim_start:
        input_args.extend(["-to", str(trim_end)])

    # Construct Filter Graph
    video_filters = []
    if aspect_ratio == "9:16":
        if bg_fill_type == "blur":
            video_filters.append("split[v1][v2];[v1]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=30[bg];[v2]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
        else:
            video_filters.append("scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(1080-iw)/2:(1920-ih)/2:color=black")
    elif aspect_ratio == "1:1":
        video_filters.append("scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(1080-iw)/2:(1080-ih)/2:color=black")

    if video_speed != 1.0:
        video_filters.append(f"setpts=(1/{video_speed})*PTS")

    video_filters.append(sub_filter)

    audio_filters = []
    if volume != 1.0:
        audio_filters.append(f"volume={volume}")
    if fade_in > 0:
        audio_filters.append(f"afade=t=in:ss=0:d={fade_in}")
    if video_speed != 1.0:
        audio_filters.append(f"atempo={video_speed}")

    vf_param = ",".join(video_filters)
    af_param = ",".join(audio_filters) if audio_filters else "anull"

    cmd = [
        ffmpeg_exe,
        "-y",
        *input_args,
        "-i",
        str(video_path.resolve()),
        "-vf",
        vf_param,
        "-af",
        af_param,
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-preset",
        "fast",
        str(output_path.resolve()),
    ]

    try:
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg error: {process.stderr}")
    finally:
        if temp_srt_path.exists():
            temp_srt_path.unlink()

    return output_path
