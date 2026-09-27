"""Scene-based video splitting service for creating natural Shorts and Reels."""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import av
import imageio_ffmpeg
import numpy as np

from .subtitles import subtitles_to_srt

logger = logging.getLogger("content_bot.scene_splitter")


class SceneSplitterError(RuntimeError):
    pass


def detect_scene_cuts(
    video_path: Path,
    *,
    threshold: float = 27.0,
    min_scene_len_s: float = 1.5,
    duration_s: float | None = None,
    check_cancel: Any | None = None,
    progress: Any | None = None,
) -> list[float]:
    """Detect scene cut timestamps (in seconds) using frame color histogram difference.

    This runs purely with PyAV without requiring external heavy packages.
    Returns list of cut timestamps in seconds: [0.0, cut_1, cut_2, ..., video_duration].
    """
    if not video_path.exists():
        raise SceneSplitterError(f"Video file not found: {video_path}")

    cuts: list[float] = [0.0]
    container = None

    try:
        container = av.open(str(video_path))
        if not container.streams.video:
            raise SceneSplitterError("Video không có luồng hình ảnh")

        stream = container.streams.video[0]
        time_base = float(stream.time_base) if stream.time_base else 1.0 / 30.0
        stream_duration_s = (
            float(stream.duration * stream.time_base)
            if stream.duration and stream.time_base
            else 0.0
        )
        total_duration_s = max(0.0, float(duration_s or stream_duration_s))

        prev_hist: np.ndarray | None = None
        last_cut_time = 0.0
        last_pts_time = 0.0

        # Sample every ~3 frames to speed up detection 3x
        frame_idx = 0
        sampled_frames = 0
        last_progress_s = -1.0
        for frame in container.decode(video=0):
            frame_idx += 1
            if check_cancel and frame_idx % 30 == 0:
                check_cancel()
            if frame_idx % 3 != 0:
                continue

            pts_s = float(frame.pts * time_base) if frame.pts is not None else float(frame.time or 0.0)
            last_pts_time = pts_s
            sampled_frames += 1
            if progress and pts_s >= last_progress_s + 1.0:
                percent = 5 if total_duration_s <= 0 else min(90, 5 + round(pts_s / total_duration_s * 85))
                progress(percent, "detecting", "Đang quét điểm chuyển cảnh")
                last_progress_s = pts_s

            # Downscaled grayscale histogram
            img_bgr = frame.to_ndarray(format="bgr24")
            gray = (
                img_bgr[::4, ::4, 0] * 0.114 + img_bgr[::4, ::4, 1] * 0.587 + img_bgr[::4, ::4, 2] * 0.299
            ).astype(np.uint8)

            hist, _ = np.histogram(gray, bins=32, range=(0, 256))
            hist = hist.astype(np.float32)
            hist_norm = hist / (np.sum(hist) + 1e-6)

            if prev_hist is not None:
                # Chi-Square or Manhattan distance between consecutive frame histograms
                diff = float(np.sum(np.abs(hist_norm - prev_hist)) * 100.0)
                if diff >= threshold and (pts_s - last_cut_time) >= min_scene_len_s:
                    cuts.append(round(pts_s, 2))
                    last_cut_time = pts_s

            prev_hist = hist_norm

        end_s = max(total_duration_s, last_pts_time)
        if end_s > cuts[-1] + 0.01:
            cuts.append(round(end_s, 2))
        if progress:
            progress(92, "grouping", f"Đã phân tích {sampled_frames} mẫu hình")

    except av.AVError as exc:
        raise SceneSplitterError(f"Lỗi đọc video khi phát hiện cảnh: {exc}") from exc
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                logger.debug("Không đóng được bộ giải mã cảnh", exc_info=True)

    return cuts


def group_scenes_into_chunks(
    scene_cuts: list[float],
    *,
    target_duration_s: float = 45.0,
    min_duration_s: float = 25.0,
    max_duration_s: float = 75.0,
) -> list[tuple[float, float]]:
    """Group contiguous scene boundaries into coherent video chunks of target duration.

    Ensures that chunks cut strictly at scene boundaries rather than abruptly mid-scene.
    Returns: [(start_s, end_s), ...]
    """
    if len(scene_cuts) < 2:
        return []

    normalized = sorted({round(float(value), 2) for value in scene_cuts})
    if normalized[0] < 0 or normalized[-1] <= normalized[0]:
        raise SceneSplitterError("Mốc cảnh không hợp lệ")
    scene_cuts = normalized

    chunks: list[tuple[float, float]] = []
    chunk_start = scene_cuts[0]

    for i in range(1, len(scene_cuts)):
        current_end = scene_cuts[i]
        curr_len = current_end - chunk_start

        is_last = i == len(scene_cuts) - 1

        if curr_len >= target_duration_s or (curr_len >= min_duration_s and is_last):
            chunks.append((round(chunk_start, 2), round(current_end, 2)))
            chunk_start = current_end
        elif curr_len > max_duration_s:
            # Force cut if a single scene or group exceeded maximum duration
            chunks.append((round(chunk_start, 2), round(current_end, 2)))
            chunk_start = current_end

    if chunk_start < scene_cuts[-1] and (scene_cuts[-1] - chunk_start) >= min_duration_s:
        chunks.append((round(chunk_start, 2), round(scene_cuts[-1], 2)))
    elif chunks and chunk_start < scene_cuts[-1]:
        # Merge trailing tail into the last chunk
        last_s, _ = chunks[-1]
        chunks[-1] = (last_s, round(scene_cuts[-1], 2))

    if not chunks:
        # A short video still needs one complete chunk; returning no result
        # would silently drop the whole input from the Shorts workflow.
        chunks.append((round(scene_cuts[0], 2), round(scene_cuts[-1], 2)))
    return chunks


def split_subtitles_for_chunk(
    document: dict[str, Any],
    start_s: float,
    end_s: float,
) -> dict[str, Any]:
    """Extract and re-timestamp subtitle cues that fall within [start_s, end_s]."""
    start_ms = round(start_s * 1000)
    end_ms = round(end_s * 1000)

    chunk_doc = copy.deepcopy(document)
    chunk_segments = []
    cue_idx = 1

    for seg in document.get("segments", []):
        seg_s = seg["start_ms"]
        seg_e = seg["end_ms"]

        # Check if cue overlaps with chunk interval
        if seg_e <= start_ms or seg_s >= end_ms:
            continue

        # Adjust timestamps relative to new chunk start (0-indexed)
        new_start_ms = max(0, seg_s - start_ms)
        new_end_ms = min(end_ms - start_ms, seg_e - start_ms)

        if new_end_ms <= new_start_ms:
            continue

        new_seg = copy.deepcopy(seg)
        new_seg["id"] = f"sub_{cue_idx:04d}"
        new_seg["start_ms"] = new_start_ms
        new_seg["end_ms"] = new_end_ms
        chunk_segments.append(new_seg)
        cue_idx += 1

    chunk_doc["segments"] = chunk_segments
    return chunk_doc


def split_video_into_shorts(
    video_path: Path,
    output_dir: Path,
    *,
    subtitles_doc: dict[str, Any] | None = None,
    target_duration_s: float = 45.0,
    min_duration_s: float = 25.0,
    max_duration_s: float = 75.0,
    ffmpeg_bin: str | None = None,
) -> list[dict[str, Any]]:
    """Split video and subtitles into scene-aware short clips."""
    scene_cuts = detect_scene_cuts(video_path)
    chunks = group_scenes_into_chunks(
        scene_cuts,
        target_duration_s=target_duration_s,
        min_duration_s=min_duration_s,
        max_duration_s=max_duration_s,
    )

    return export_scene_chunks(
        video_path,
        output_dir,
        chunks,
        subtitles_doc=subtitles_doc,
        ffmpeg_bin=ffmpeg_bin,
    )


def export_scene_chunks(
    video_path: Path,
    output_dir: Path,
    chunks: list[tuple[float, float]],
    *,
    subtitles_doc: dict[str, Any] | None = None,
    source_fingerprint: str | None = None,
    ffmpeg_bin: str | None = None,
    check_cancel: Any | None = None,
    progress: Any | None = None,
) -> list[dict[str, Any]]:
    """Export already-approved scene bounds and write a reproducible manifest.

    Bounds are validated before any process starts. Video files are written to a
    temporary sibling and atomically renamed, while subtitle JSON/SRT and the
    manifest use paths relative to ``output_dir`` so they can be moved together.
    """
    if not video_path.is_file():
        raise SceneSplitterError("Video file not found")
    if not chunks:
        raise SceneSplitterError("Không có đoạn cảnh để xuất")

    normalized: list[tuple[float, float]] = []
    previous_end = -1.0
    for start_s, end_s in chunks:
        start_s = round(float(start_s), 2)
        end_s = round(float(end_s), 2)
        if start_s < 0 or end_s <= start_s or start_s < previous_end:
            raise SceneSplitterError("Mốc cảnh không hợp lệ hoặc chồng lấn")
        normalized.append((start_s, end_s))
        previous_end = end_s

    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_bin or shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
    results: list[dict[str, Any]] = []

    stem = video_path.stem
    manifest_path = output_dir / f"{stem}_manifest.json"
    for idx, (c_start, c_end) in enumerate(normalized, start=1):
        if check_cancel:
            check_cancel()
        chunk_video = output_dir / f"{stem}_short_{idx:02d}.mp4"
        temporary_video = chunk_video.with_name(f"{chunk_video.stem}.tmp.mp4")
        duration = c_end - c_start

        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-ss",
            f"{c_start:.2f}",
            "-t",
            f"{duration:.2f}",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-c:a",
            "aac",
            str(temporary_video),
        ]

        try:
            subprocess.run(cmd, capture_output=True, check=True)
            os.replace(temporary_video, chunk_video)
        except subprocess.CalledProcessError as exc:
            temporary_video.unlink(missing_ok=True)
            detail = exc.stderr.decode("utf-8", errors="replace")[-500:]
            raise SceneSplitterError(f"Không thể xuất đoạn cảnh: {detail}") from exc

        chunk_info: dict[str, Any] = {
            "chunk_index": idx,
            "start_s": c_start,
            "end_s": c_end,
            "duration_s": round(duration, 2),
            "video_path": str(chunk_video),
            "video_file": chunk_video.name,
        }

        if subtitles_doc:
            chunk_subs = split_subtitles_for_chunk(subtitles_doc, c_start, c_end)
            chunk_sub_path = output_dir / f"{stem}_short_{idx:02d}.json"
            chunk_srt_path = output_dir / f"{stem}_short_{idx:02d}.srt"
            chunk_subs_json = json.dumps(chunk_subs, ensure_ascii=False, indent=2)
            chunk_sub_path.write_text(
                chunk_subs_json, encoding="utf-8"
            )
            chunk_srt_path.write_text(
                subtitles_to_srt(chunk_subs.get("segments", [])), encoding="utf-8"
            )
            chunk_info["subtitles_path"] = str(chunk_sub_path)
            chunk_info["subtitles_file"] = chunk_sub_path.name
            chunk_info["srt_path"] = str(chunk_srt_path)
            chunk_info["srt_file"] = chunk_srt_path.name
            chunk_info["cue_count"] = len(chunk_subs.get("segments", []))

        results.append(chunk_info)
        if progress:
            progress(round(idx / len(normalized) * 90), "exporting", f"Đã xuất {idx}/{len(normalized)} đoạn")

    manifest = {
        "schema_version": 1,
        "source_video": video_path.name,
        "source_fingerprint": source_fingerprint,
        "chunks": [
            {key: value for key, value in item.items() if key not in {"video_path", "subtitles_path", "srt_path"}}
            for item in results
        ],
    }
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary_manifest, manifest_path)
    for item in results:
        item["manifest_path"] = str(manifest_path)
        item["manifest_file"] = manifest_path.name

    return results
