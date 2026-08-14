from __future__ import annotations

import re
import subprocess
import threading
from decimal import Decimal
from pathlib import Path

import imageio_ffmpeg
import pytest
from pydantic import ValidationError

from app.schemas import SubtitleRenderOptionsV2
from app.services.media_probe import probe_media
from app.services.subtitle_render import (
    SubtitleRenderCanceled,
    _audio_arguments,
    _force_style,
    _output_duration_ms,
    _target_dimensions,
    _video_filter_graph,
    encoder_candidates,
    precision_render_cache_key,
    render_precision_video,
)
from app.services.subtitles import subtitles_to_ass, subtitles_to_srt


def _document(start_ms: int = 1001, end_ms: int = 1501) -> dict:
    return {
        "schema_version": 2,
        "language": "vi",
        "timebase": "milliseconds",
        "timing_source": "manual",
        "timing_precision_ms": 1,
        "segments": [
            {
                "id": "s1",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": "Phụ đề 1 ms",
                "timing_source": "manual",
                "timing_precision_ms": 1,
                "confidence": None,
                "needs_review": False,
                "revision": 1,
            }
        ],
    }


def _options(**updates) -> dict:
    base = SubtitleRenderOptionsV2(encoder="software").model_dump(mode="json")
    return {**base, **updates}


def test_render_cache_key_changes_for_timing_style_and_profile() -> None:
    media = {"fingerprint": "a" * 64, "audio_hash": "b" * 64}
    base = precision_render_cache_key(_document(), media, _options())

    assert precision_render_cache_key(_document(1002), media, _options()) != base
    assert (
        precision_render_cache_key(_document(), media, _options(font_color="#FF0000"))
        != base
    )
    assert (
        precision_render_cache_key(_document(), media, _options(profile="quality"))
        != base
    )
    overlay = {"overlay_id": "c" * 64, "x": 14, "y": 12, "width": 22}
    assert precision_render_cache_key(_document(), media, _options(), overlay) != base
    assert (
        precision_render_cache_key(
            _document(), media, _options(), {**overlay, "width": 23}
        )
        != precision_render_cache_key(_document(), media, _options(), overlay)
    )
    masks = [{"id": "mask-1", "shape": "rectangle", "effect": "blur"}]
    assert precision_render_cache_key(_document(), media, _options(), None, masks) != base


def test_encoder_selection_has_runtime_fallback_order() -> None:
    capabilities = {"h264_nvenc": False, "h264_qsv": True, "libx264": True}
    assert encoder_candidates("auto", capabilities) == ["h264_qsv", "libx264"]
    assert encoder_candidates("software", capabilities) == ["libx264"]


def test_audio_is_copied_only_when_unchanged_and_fade_out_is_direct() -> None:
    media = {"has_audio": True, "audio_codec": "aac"}
    copied_args, copied = _audio_arguments(media, _options(), 5000)
    assert copied is True
    assert copied_args[-1] == "copy"

    filtered_args, copied = _audio_arguments(
        media,
        _options(fade_out_ms=500, volume=0.8),
        5000,
    )
    assert copied is False
    filter_graph = filtered_args[filtered_args.index("-af") + 1]
    assert "afade=t=out:st=4.500:d=0.500" in filter_graph
    assert "areverse" not in filter_graph

    pcm_args, copied = _audio_arguments(
        {"has_audio": True, "audio_codec": "pcm_s16le"},
        _options(),
        5000,
    )
    assert copied is False
    assert pcm_args[pcm_args.index("-c:a") + 1] == "aac"


def test_output_duration_uses_single_decimal_time_map_rounding() -> None:
    media = {"duration_ms": 10_000}
    options = _options(
        trim_start_ms=1001,
        trim_end_ms=9001,
        video_speed=str(Decimal("1.25")),
    )
    assert _output_duration_ms(media, options) == 6400


def test_precision_style_matches_browser_font_metrics_and_custom_anchor() -> None:
    style = _force_style(
        _options(
            font_size=40,
            outline_width=2,
            shadow_width=1,
            spacing=5,
            pos_x=25,
            pos_y=75,
            alignment_type="center",
        )
    )

    assert "FontSize=24.0" in style
    # Outline, shadow and spacing are geometric units; unlike FontSize, they
    # must not receive the 1.5 libass font-metric correction.
    assert "Outline=0.8" in style
    assert "Shadow=0.4" in style
    assert "Spacing=2.0" in style
    assert "Alignment=2" in style
    assert "MarginL=4" in style
    assert "MarginR=196" in style


def test_mask_filter_graph_supports_all_shapes_and_effects(tmp_path: Path) -> None:
    width, height = 320, 180
    subtitle_path = tmp_path / "mask.srt"
    subtitle_path.write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nKiểm thử\n",
        encoding="utf-8",
    )
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo"
    masks = [
        {
            "id": "mask-rectangle",
            "shape": "rectangle",
            "effect": "blur",
            "x": 10,
            "y": 10,
            "width": 25,
            "height": 15,
            "strength": 8,
            "opacity": 0.85,
            "feather": 2,
            "corner_radius": 14,
            "color": "#000000",
        },
        {
            "id": "mask-rounded",
            "shape": "rounded",
            "effect": "pixelate",
            "x": 40,
            "y": 10,
            "width": 25,
            "height": 15,
            "strength": 12,
            "opacity": 1,
            "feather": 0,
            "corner_radius": 20,
            "color": "#000000",
        },
        {
            "id": "mask-ellipse",
            "shape": "ellipse",
            "effect": "solid",
            "x": 10,
            "y": 40,
            "width": 25,
            "height": 15,
            "strength": 8,
            "opacity": 0.7,
            "feather": 1,
            "corner_radius": 14,
            "color": "#123456",
        },
        {
            "id": "mask-band",
            "shape": "band",
            "effect": "darken",
            "x": 0,
            "y": 75,
            "width": 100,
            "height": 10,
            "strength": 8,
            "opacity": 0.6,
            "feather": 0,
            "corner_radius": 14,
            "color": "#000000",
        },
    ]
    graph, output_label = _video_filter_graph(
        subtitle_path,
        fonts_dir,
        {"width": width, "height": height},
        _options(),
        masks=masks,
    )

    assert graph.index("subtitle_mask_0") < graph.index("subtitles=filename")
    assert graph.count("255*1.0000") == 2
    assert "255*0.7000" in graph
    assert "color=0x000000@0.6000" in graph
    assert "crop=96:44:24:10" in graph
    assert "scale=320:180:flags=neighbor" not in graph
    rendered = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s={width}x{height}:r=1:d=1",
            "-filter_complex",
            graph,
            "-map",
            output_label,
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr


def test_mask_filter_preserves_source_color(tmp_path: Path) -> None:
    width, height = 160, 90
    subtitle_path = tmp_path / "color.srt"
    subtitle_path.write_text(
        "1\n00:00:02,000 --> 00:00:03,000\nKhông hiện ở frame kiểm thử\n",
        encoding="utf-8",
    )
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo"
    graph, output_label = _video_filter_graph(
        subtitle_path,
        fonts_dir,
        {"width": width, "height": height},
        _options(),
        masks=[{
            "id": "mask-color",
            "shape": "rectangle",
            "effect": "blur",
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 100,
            "strength": 20,
            "opacity": 1,
            "feather": 0,
            "corner_radius": 0,
            "color": "#000000",
        }],
    )
    rendered = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s={width}x{height}:r=1:d=1",
            "-filter_complex",
            graph,
            "-map",
            output_label,
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr.decode(errors="replace")
    center_offset = ((height // 2) * width + width // 2) * 3
    red, green, blue = rendered.stdout[center_offset : center_offset + 3]
    assert red > 220
    assert green < 40
    assert blue < 40


def _changed_pixel_bounds(
    frame: bytes,
    *,
    width: int,
    height: int,
    threshold: int = 8,
) -> tuple[int, int, int, int]:
    background = frame[:3]
    min_x, min_y = width, height
    max_x = max_y = -1
    for pixel_index in range(width * height):
        offset = pixel_index * 3
        if max(
            abs(frame[offset + channel] - background[channel]) for channel in range(3)
        ) <= threshold:
            continue
        x = pixel_index % width
        y = pixel_index // width
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
    assert max_x >= min_x and max_y >= min_y
    return min_x, min_y, max_x, max_y


def _bright_pixel_bounds(
    frame: bytes,
    *,
    width: int,
    height: int,
    threshold: int = 180,
) -> tuple[int, int, int, int]:
    min_x, min_y = width, height
    max_x = max_y = -1
    for pixel_index in range(width * height):
        offset = pixel_index * 3
        if max(frame[offset : offset + 3]) <= threshold:
            continue
        x = pixel_index % width
        y = pixel_index // width
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
    assert max_x >= min_x and max_y >= min_y
    return min_x, min_y, max_x, max_y


@pytest.mark.parametrize("subtitle_format", ["srt", "ass"])
def test_render_geometry_matches_desktop_live_preview(
    tmp_path: Path,
    subtitle_format: str,
) -> None:
    """Keep libass within 1.5% of the measured Chromium preview geometry."""
    width, height = 1280, 720
    options = _options(
        font_size=38,
        bold=True,
        bg_enabled=True,
        outline_width=2,
        shadow_width=2,
        pos_x=50,
        pos_y=78,
    )
    subtitle_path = tmp_path / f"sync.{subtitle_format}"
    if subtitle_format == "srt":
        subtitle_data = subtitles_to_srt(
            [
                {
                    "start_ms": 0,
                    "end_ms": 5000,
                    "text": "KHẢ KHẢ, TRÀ TRÀ, SAO CÁC CẬU LẠI...",
                }
            ]
        )
    else:
        subtitle_data = subtitles_to_ass(
            [
                {
                    "start_seconds": 0,
                    "end_seconds": 5,
                    "text": "KHẢ KHẢ, TRÀ TRÀ, SAO CÁC CẬU LẠI...",
                }
            ],
            options,
        )
    subtitle_path.write_text(subtitle_data, encoding="utf-8")
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo"
    graph, output_label = _video_filter_graph(
        subtitle_path,
        fonts_dir,
        {"width": width, "height": height},
        options,
    )
    rendered = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x808080:s={width}x{height}:r=1:d=1",
            "-filter_complex",
            graph,
            "-map",
            output_label,
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert len(rendered.stdout) == width * height * 3
    min_x, min_y, max_x, max_y = _changed_pixel_bounds(
        rendered.stdout,
        width=width,
        height=height,
    )
    normalized_width = (max_x - min_x + 1) / width
    normalized_height = (max_y - min_y + 1) / height
    text_min_x, text_min_y, text_max_x, text_max_y = _bright_pixel_bounds(
        rendered.stdout,
        width=width,
        height=height,
    )
    normalized_text_width = (text_max_x - text_min_x + 1) / width
    normalized_text_height = (text_max_y - text_min_y + 1) / height
    # Chromium reference at 1440x900: 0.6117 x 0.0789 of the video frame.
    assert normalized_width == pytest.approx(0.6117, abs=0.015)
    assert normalized_height == pytest.approx(0.0789, abs=0.015)
    # Canvas TextMetrics reference: 0.5885 x 0.0591. Keep the painted glyphs
    # tighter than the background-box tolerance because this is perceived size.
    assert normalized_text_width == pytest.approx(0.5885, abs=0.007)
    assert normalized_text_height == pytest.approx(0.0591, abs=0.003)


def test_libass_preview_matches_precision_export_at_minimum_font_size(
    tmp_path: Path,
) -> None:
    width, height = 1280, 720
    options = _options(
        font_size=14,
        bold=True,
        bg_enabled=True,
        outline_width=2,
        shadow_width=1,
        pos_x=50,
        pos_y=78,
    )
    cue = {
        "start_ms": 0,
        "end_ms": 5000,
        "text": "Em muốn trở thành con rồng mạnh nhất thế giới.",
    }
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo"
    bounds: list[tuple[int, int, int, int]] = []

    for subtitle_format in ("srt", "ass"):
        subtitle_path = tmp_path / f"minimum-font.{subtitle_format}"
        subtitle_path.write_text(
            subtitles_to_srt([cue])
            if subtitle_format == "srt"
            else subtitles_to_ass([cue], options),
            encoding="utf-8",
        )
        graph, output_label = _video_filter_graph(
            subtitle_path,
            fonts_dir,
            {"width": width, "height": height},
            options,
        )
        frame = subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x808080:s={width}x{height}:r=1:d=1",
                "-filter_complex",
                graph,
                "-map",
                output_label,
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        bounds.append(
            _changed_pixel_bounds(frame, width=width, height=height)
        )

    for preview_value, export_value in zip(bounds[1], bounds[0], strict=True):
        assert preview_value == pytest.approx(export_value, abs=2)


def test_aspect_targets_keep_even_display_dimensions() -> None:
    landscape = {"width": 1920, "height": 1080}
    portrait = {"width": 720, "height": 1280}
    assert _target_dimensions(landscape, "original") is None
    assert _target_dimensions(landscape, "9:16") == (1080, 1920)
    assert _target_dimensions(landscape, "1:1") == (1080, 1080)
    assert _target_dimensions(portrait, "16:9") == (1280, 720)


def test_precision_schema_refuses_centisecond_effects() -> None:
    with pytest.raises(ValidationError):
        SubtitleRenderOptionsV2(render_mode="precision", animation="fade")

    effects = SubtitleRenderOptionsV2(render_mode="effects", animation="fade")
    assert effects.animation == "fade"
    assert effects.render_mode == "effects"


def _decoded_frame_crcs(video_path: Path) -> tuple[tuple[int, int], dict[int, str]]:
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-vf",
            "format=gray",
            "-f",
            "framecrc",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    numerator, denominator = 0, 0
    frames: dict[int, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("#tb 0:"):
            numerator, denominator = (
                int(value) for value in line.split(":", 1)[1].strip().split("/")
            )
        elif line and not line.startswith("#"):
            fields = [field.strip() for field in line.split(",")]
            frames[int(fields[2])] = fields[5]
    return (numerator, denominator), frames


def _frame_luma_max_by_ms(video_path: Path) -> dict[int, int]:
    """Measure subtitle presence without relying on lossy H.264 frame CRCs."""
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-vf",
            "signalstats,metadata=print:file=-",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    frames: dict[int, int] = {}
    current_ms: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("frame:"):
            match = re.search(r"pts_time:([0-9.]+)", line)
            current_ms = round(float(match.group(1)) * 1000) if match else None
        elif current_ms is not None and line.startswith("lavfi.signalstats.YMAX="):
            frames[current_ms] = int(line.rsplit("=", 1)[1])
    assert frames
    return frames


def test_precision_render_switches_on_first_valid_frame_and_is_atomic(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
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
            "color=c=black:s=640x360:r=30:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    output_dir = tmp_path / "output"
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo"

    first = render_precision_video(
        source,
        _document(),
        media,
        _options(),
        output_dir,
        video_id="a" * 12,
        fonts_dir=fonts_dir,
        timeout_seconds=60,
    )
    rendered = output_dir / first["output_filename"]
    (time_base_numerator, time_base_denominator), _ = _decoded_frame_crcs(rendered)
    luma = _frame_luma_max_by_ms(rendered)
    assert (time_base_numerator, time_base_denominator) == (1, 30)

    # Cue [1001, 1501): frame 1000 ms is too early; 1033 and 1500 ms are active;
    # frame 1533 ms is after the exclusive end.
    assert luma[1000] < 32
    assert luma[1033] > 64
    assert luma[1500] > 64
    assert luma[1533] < 32
    assert not list(tmp_path.rglob("*.part*"))

    second = render_precision_video(
        source,
        _document(),
        media,
        _options(),
        output_dir,
        video_id="a" * 12,
        fonts_dir=fonts_dir,
        timeout_seconds=60,
    )
    assert second["cache_hit"] is True
    assert second["output_filename"] == first["output_filename"]


@pytest.mark.parametrize(
    "rate",
    ["24000/1001", "24", "25", "30000/1001", "30", "50", "60000/1001", "60"],
)
def test_precision_render_cfr_frame_rate_matrix(tmp_path: Path, rate: str) -> None:
    source = tmp_path / "cfr-source.mp4"
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
            f"color=c=black:s=160x90:r={rate}:d=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    assert media["is_vfr"] is False

    result = render_precision_video(
        source,
        _document(101, 251),
        media,
        _options(),
        tmp_path / "output",
        video_id="e" * 12,
        fonts_dir=Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo",
        timeout_seconds=60,
    )
    frames = _frame_luma_max_by_ms(tmp_path / "output" / result["output_filename"])
    active = [
        luma_max
        for milliseconds, luma_max in frames.items()
        if 101 <= milliseconds < 251
    ]
    inactive = [
        luma_max
        for milliseconds, luma_max in frames.items()
        if milliseconds < 101 or milliseconds >= 251
    ]

    assert active and all(luma_max > 64 for luma_max in active)
    assert inactive and all(luma_max < 32 for luma_max in inactive)


def test_effects_render_uses_explicit_centisecond_compatibility_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "effects-source.mp4"
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
            "color=c=black:s=320x180:r=30:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    result = render_precision_video(
        source,
        _document(101, 801),
        media,
        _options(render_mode="effects", animation="fade"),
        tmp_path / "output",
        video_id="c" * 12,
        fonts_dir=Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo",
        timeout_seconds=60,
    )

    assert result["render_mode"] == "effects"
    assert result["timing_precision_ms"] == 10
    assert any(
        warning["code"] == "effects_timing_quantized" for warning in result["warnings"]
    )
    assert (tmp_path / "output" / result["output_filename"]).is_file()


def test_precision_render_burns_overlay_at_preview_position_and_size(
    tmp_path: Path,
) -> None:
    source = tmp_path / "overlay-source.mp4"
    logo = tmp_path / "logo.png"
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x360:r=30:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=100x50:d=0.1,format=rgba",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-threads",
            "1",
            "-update",
            "1",
            str(logo),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    overlay = {"overlay_id": "d" * 64, "x": 20, "y": 20, "width": 25}
    result = render_precision_video(
        source,
        _document(1, 900),
        media,
        _options(),
        tmp_path / "output",
        video_id="f" * 12,
        fonts_dir=Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo",
        overlay_path=logo,
        overlay=overlay,
        timeout_seconds=60,
    )
    assert result["overlay_applied"] is True
    rendered_path = tmp_path / "output" / result["output_filename"]
    frame = subprocess.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(rendered_path),
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    red_pixels: list[tuple[int, int]] = []
    for pixel_index in range(640 * 360):
        offset = pixel_index * 3
        red, green, blue = frame[offset : offset + 3]
        if red > 160 and green < 80 and blue < 80:
            red_pixels.append((pixel_index % 640, pixel_index // 640))
    assert red_pixels
    xs = [pixel[0] for pixel in red_pixels]
    ys = [pixel[1] for pixel in red_pixels]
    # Preview semantics: width 25% = 160 px; center (20%, 20%) = (128, 72).
    assert min(xs) == pytest.approx(48, abs=4)
    assert max(xs) == pytest.approx(207, abs=4)
    assert min(ys) == pytest.approx(32, abs=4)
    assert max(ys) == pytest.approx(111, abs=4)


def test_precision_render_preserves_vfr_pts_and_frame_boundaries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "vfr-source.mp4"
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
            "color=c=black:s=320x180:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=30:d=1",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    assert media["is_vfr"] is True
    assert {958, 1000, 1467, 1500, 1533}.issubset(media["frame_pts_ms"])

    result = render_precision_video(
        source,
        _document(1000, 1500),
        media,
        _options(),
        tmp_path / "output",
        video_id="d" * 12,
        fonts_dir=Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo",
        timeout_seconds=60,
    )
    rendered = tmp_path / "output" / result["output_filename"]
    output_media = probe_media(rendered)
    frames = _frame_luma_max_by_ms(rendered)

    assert output_media["is_vfr"] is True
    # The VFR source contains an exact 1.500000 s frame. Since cue ranges are
    # [start, end), that frame and the following one must both be inactive.
    assert frames[958] < 32
    assert frames[1000] > 64
    assert frames[1467] > 64
    assert frames[1500] < 32
    assert frames[1533] < 32


def test_render_cancel_removes_partial_output_and_work_files(tmp_path: Path) -> None:
    source = tmp_path / "cancel-source.mp4"
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
            "testsrc2=s=1280x720:r=60:d=5",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(source)
    output_dir = tmp_path / "output"
    cancel_event = threading.Event()
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo"

    def cancel_on_progress(progress: int, _phase: str, _message: str) -> None:
        if progress >= 5:
            cancel_event.set()

    with pytest.raises(SubtitleRenderCanceled):
        render_precision_video(
            source,
            _document(100, 4900),
            media,
            _options(),
            output_dir,
            video_id="b" * 12,
            fonts_dir=fonts_dir,
            cancel_event=cancel_event,
            progress=cancel_on_progress,
            timeout_seconds=60,
        )

    assert not list(tmp_path.rglob("*.part*"))
    assert not list((tmp_path / "render-work").rglob("*"))
