from __future__ import annotations

import argparse
import csv
import ctypes
import difflib
import hashlib
import html
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import av

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VIDEO = Path("data/videos/upload/d8b5b6f8fa554a0dad2d84b65c4f0f28.mp4")
DEFAULT_BASELINE = Path(
    "artifacts/subtitle-remediation/phase7-ocr/ocr-00-metrics-20260920/gpu.json"
)
DEFAULT_COMPARISON = Path(
    "artifacts/subtitle-remediation/phase7-ocr/hardware-fps1/gpu.json"
)
DEFAULT_OUTPUT = Path(
    "artifacts/subtitle-remediation/phase7-ocr/ocr-00-ground-truth-review-20260920-v5-blind-first-final-r3"
)
SWEEP_WINDOW_MS = 5_000


@dataclass(frozen=True)
class FrameTarget:
    key: str
    requested_ms: int


def resolve_path(value: Path) -> Path:
    return value if value.is_absolute() else REPO_ROOT / value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_snapshot() -> dict[str, Any]:
    source_paths = [
        Path("backend/app/services/subtitle_ocr.py"),
        Path("backend/app/services/subtitle_ocr_worker.py"),
        Path("backend/app/services/subtitle_ocr_supervisor.py"),
        Path("backend/scripts/benchmark_subtitle_ocr_hardware.py"),
        Path("backend/scripts/install_subtitle_ocr_gpu_runtime.ps1"),
        Path("backend/runtimes/ocr_gpu/requirements.txt"),
        Path("backend/scripts/build_subtitle_ocr_review_pack.py"),
    ]
    fingerprints = [
        {"path": str(path), "sha256": sha256_file(REPO_ROOT / path)}
        for path in source_paths
        if (REPO_ROOT / path).is_file()
    ]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    status = subprocess.run(
        ["git", "status", "--short", "--", *(str(path) for path in source_paths)],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    return {
        "capture_scope": (
            "Current files while building this review package. Historical benchmark JSON "
            "does not contain a git revision or source-file fingerprints."
        ),
        "git_head_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "git_status_for_source_paths": status.stdout.splitlines()
        if status.returncode == 0
        else None,
        "files": fingerprints,
    }


def environment_snapshot() -> dict[str, Any]:
    profile_root = REPO_ROOT / "backend/runtimes/ocr_gpu"
    profile_manifest = profile_root / "site-packages/runtime-manifest.json"
    profile_requirements = profile_root / "requirements.txt"
    profile_manifest_data = None
    if profile_manifest.is_file():
        profile_manifest_data = json.loads(profile_manifest.read_text(encoding="utf-8"))
    requirement_pins = []
    if profile_requirements.is_file():
        requirement_pins = [
            line.strip()
            for line in profile_requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    model_files = []
    rapidocr_spec = importlib.util.find_spec("rapidocr_onnxruntime")
    if rapidocr_spec and rapidocr_spec.submodule_search_locations:
        for package_root in rapidocr_spec.submodule_search_locations:
            model_dir = Path(package_root) / "models"
            if model_dir.is_dir():
                model_files.extend(
                    {
                        "path": str(path.resolve()),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                    for path in sorted(model_dir.glob("*.onnx"))
                )

    gpu_devices = []
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,uuid,memory.total,driver_version,temperature.gpu,utilization.gpu,clocks.current.graphics,clocks.current.memory,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=8,
        )
        if result.returncode == 0:
            for row in result.stdout.splitlines():
                values = [value.strip() for value in row.split(",")]
                if len(values) == 9:
                    gpu_devices.append(
                        {
                            "name": values[0],
                            "uuid": values[1],
                            "memory_total_mib": values[2],
                            "driver_version": values[3],
                            "temperature_c": values[4],
                            "utilization_percent": values[5],
                            "graphics_clock_mhz": values[6],
                            "memory_clock_mhz": values[7],
                            "power_draw_w": values[8],
                        }
                    )
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        rapidocr_version = importlib.metadata.version("rapidocr_onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        rapidocr_version = None
    try:
        base_ort_version = importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        base_ort_version = None

    windows_hardware: dict[str, Any] = {}
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as registry_key:
                windows_hardware["cpu_name"] = winreg.QueryValueEx(
                    registry_key, "ProcessorNameString"
                )[0].strip()
                windows_hardware["cpu_current_mhz"] = winreg.QueryValueEx(
                    registry_key, "~MHz"
                )[0]
        except OSError:
            pass
        try:
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            memory = MemoryStatus()
            memory.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                windows_hardware["physical_memory_bytes"] = memory.ullTotalPhys
                windows_hardware["physical_memory_load_percent"] = memory.dwMemoryLoad
        except (AttributeError, OSError):
            pass
        try:
            power = subprocess.run(
                ["powercfg", "/getactivescheme"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            windows_hardware["active_power_scheme"] = (
                power.stdout.strip() if power.returncode == 0 else None
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    return {
        "captured_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "operating_system": platform.platform(),
        "machine_architecture": platform.machine(),
        "cpu_processor_string": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "windows_hardware_snapshot": windows_hardware,
        "current_backend_base_onnxruntime_version": base_ort_version,
        "current_rapidocr_onnxruntime_version": rapidocr_version,
        "gpu_devices_at_package_build": gpu_devices,
        "worker_runtime_profile": {
            "manifest_path": str(profile_manifest.resolve())
            if profile_manifest.is_file()
            else None,
            "manifest_sha256": sha256_file(profile_manifest)
            if profile_manifest.is_file()
            else None,
            "manifest": profile_manifest_data,
            "requirements_path": str(profile_requirements.resolve())
            if profile_requirements.is_file()
            else None,
            "requirements_sha256": sha256_file(profile_requirements)
            if profile_requirements.is_file()
            else None,
            "requirements_pins": requirement_pins,
        },
        "model_files_available_at_package_build": model_files,
        "source_snapshot": source_snapshot(),
        "historical_run_limit": (
            "Host, source, and model fingerprints in this section describe the current "
            "package-build environment. The baseline metrics artifact did not record "
            "these fingerprints at the time it ran."
        ),
    }


def read_metrics(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("segments"), list):
        raise TypeError(f"Metrics file has no segments array: {path}")
    return data


def candidate_id(source: str, index: int, run_id: str | None = None) -> str:
    prefix = f"{run_id}-" if run_id else ""
    return f"{prefix}{source}-{index:03d}"


def frame_targets(
    candidates: list[dict[str, Any]],
    *,
    source: str,
    cue_ids: set[str] | None = None,
) -> list[FrameTarget]:
    targets: list[FrameTarget] = []
    for candidate in candidates:
        if cue_ids is not None and candidate["candidate_id"] not in cue_ids:
            continue
        start = int(candidate["start_ms"])
        end = int(candidate["end_ms"])
        if source == "fps3":
            offsets = [
                ("start_minus_250", max(0, start - 250)),
                ("start_at", start),
                ("start_plus_250", start + 250),
                ("middle", (start + end) // 2),
                ("end_minus_250", max(0, end - 250)),
                ("end_at", end),
                ("end_plus_250", end + 250),
            ]
        else:
            offsets = [
                ("start", start),
                ("middle", (start + end) // 2),
                ("end", end),
            ]
        targets.extend(
            FrameTarget(f"{candidate['candidate_id']}_{name}", requested)
            for name, requested in offsets
        )
    return targets


def crop_and_save(frame: av.VideoFrame, roi: dict[str, float], destination: Path) -> None:
    image = frame.to_image()
    width, height = image.size
    left = round(width * roi["x"] / 100)
    top = round(height * roi["y"] / 100)
    right = min(width, round(width * (roi["x"] + roi["width"]) / 100))
    bottom = min(height, round(height * (roi["y"] + roi["height"]) / 100))
    image.crop((left, top, right, bottom)).save(
        destination, format="JPEG", quality=91, optimize=True
    )


def resolve_video_duration_ms(
    *,
    first_absolute_ms: float,
    last_frame_start_ms: float,
    stream_start_time: int | None,
    stream_duration: int | None,
    stream_time_base: Any,
    last_frame_pts: int,
    last_frame_duration: int | None,
    last_frame_time_base: Any,
    fallback_frame_interval_ms: float,
) -> tuple[int, str]:
    end_candidates: list[tuple[str, float]] = []
    if stream_duration is not None and stream_time_base is not None:
        stream_start_ms = (
            float(stream_start_time * stream_time_base) * 1000
            if stream_start_time is not None
            else first_absolute_ms
        )
        stream_duration_ms = float(stream_duration * stream_time_base) * 1000
        stream_end_ms = stream_start_ms + stream_duration_ms - first_absolute_ms
        if stream_end_ms > last_frame_start_ms:
            end_candidates.append(("stream_start_plus_duration", stream_end_ms))

    if (
        last_frame_duration is not None
        and last_frame_duration > 0
        and last_frame_time_base is not None
    ):
        frame_end_ms = (
            float((last_frame_pts + last_frame_duration) * last_frame_time_base) * 1000
            - first_absolute_ms
        )
        if frame_end_ms > last_frame_start_ms:
            end_candidates.append(("last_frame_pts_plus_duration", frame_end_ms))

    if not end_candidates:
        end_candidates.append(
            ("frame_interval_fallback", last_frame_start_ms + max(1.0, fallback_frame_interval_ms))
        )
    duration_end_ms = max(value for _, value in end_candidates)
    method = "+".join(name for name, _ in end_candidates)
    return max(1, math.ceil(duration_end_ms)), method


def extract_review_frames(
    video_path: Path,
    targets: list[FrameTarget],
    roi: dict[str, float],
    output_dir: Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    target_rows = sorted(targets, key=lambda item: item.requested_ms)
    captured: dict[str, dict[str, Any]] = {}
    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        previous: av.VideoFrame | None = None
        previous_ms: float | None = None
        first_ms: float | None = None
        first_pts: int | None = None
        cursor = 0
        last_frame: av.VideoFrame | None = None
        last_ms = 0.0
        frame_intervals_ms: list[float] = []

        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            absolute_ms = float(frame.pts * frame.time_base) * 1000
            if first_ms is None:
                first_ms = absolute_ms
                first_pts = frame.pts
            current_ms = absolute_ms - first_ms
            if previous_ms is not None and current_ms > previous_ms:
                frame_intervals_ms.append(current_ms - previous_ms)
            last_frame, last_ms = frame, current_ms

            while (
                cursor < len(target_rows)
                and target_rows[cursor].requested_ms <= current_ms
            ):
                target = target_rows[cursor]
                chosen = frame
                chosen_ms = current_ms
                if (
                    previous is not None
                    and previous_ms is not None
                    and abs(previous_ms - target.requested_ms)
                    <= abs(current_ms - target.requested_ms)
                ):
                    chosen, chosen_ms = previous, previous_ms
                if output_dir is None:
                    raise ValueError("output_dir is required when extracting review frames")
                filename = f"{target.key}.jpg"
                crop_and_save(chosen, roi, output_dir / filename)
                captured[target.key] = {
                    "file": f"frames/{filename}",
                    "requested_ms": target.requested_ms,
                    "frame_pts_ms": round(chosen_ms, 3),
                    "delta_ms": round(chosen_ms - target.requested_ms, 3),
                    "pts": chosen.pts,
                    "time_base": str(chosen.time_base),
                }
                cursor += 1

            previous, previous_ms = frame, current_ms

        if last_frame is None or first_ms is None:
            raise ValueError(f"The video has no decodable frames: {video_path}")

        while cursor < len(target_rows):
            target = target_rows[cursor]
            if output_dir is None:
                raise ValueError("output_dir is required when extracting review frames")
            filename = f"{target.key}.jpg"
            crop_and_save(last_frame, roi, output_dir / filename)
            captured[target.key] = {
                "file": f"frames/{filename}",
                "requested_ms": target.requested_ms,
                "frame_pts_ms": round(last_ms, 3),
                "delta_ms": round(last_ms - target.requested_ms, 3),
                "pts": last_frame.pts,
                "time_base": str(last_frame.time_base),
            }
            cursor += 1

        if frame_intervals_ms:
            fallback_interval_ms = statistics.median(frame_intervals_ms[-200:])
        else:
            try:
                fallback_interval_ms = 1000 / float(stream.average_rate)
            except (TypeError, ValueError, ZeroDivisionError):
                fallback_interval_ms = 1.0
        duration_ms, duration_method = resolve_video_duration_ms(
            first_absolute_ms=first_ms,
            last_frame_start_ms=last_ms,
            stream_start_time=stream.start_time,
            stream_duration=stream.duration,
            stream_time_base=stream.time_base,
            last_frame_pts=last_frame.pts,
            last_frame_duration=last_frame.duration,
            last_frame_time_base=last_frame.time_base,
            fallback_frame_interval_ms=fallback_interval_ms,
        )
        stream_info = {
            "codec": stream.codec_context.name,
            "width": stream.width,
            "height": stream.height,
            "average_rate": str(stream.average_rate),
            "time_base": str(stream.time_base),
            "start_time": stream.start_time,
            "duration_ms_from_first_frame": duration_ms,
            "duration_method": duration_method,
            "median_recent_frame_interval_ms": round(fallback_interval_ms, 3),
            "last_frame_start_ms_from_first_frame": round(last_ms, 3),
            "first_frame_pts": first_pts,
            "last_frame_pts": last_frame.pts,
        }
    return captured, stream_info


def build_candidates(
    metrics: dict[str, Any], source: str, run_id: str | None = None
) -> list[dict[str, Any]]:
    result = []
    for index, segment in enumerate(metrics["segments"], start=1):
        result.append(
            {
                "candidate_id": candidate_id(source, index, run_id),
                "run_id": run_id or "unversioned",
                "source": source,
                "start_ms": int(segment["start_ms"]),
                "end_ms": int(segment["end_ms"]),
                "text": str(segment.get("text", "")),
                "confidence": segment.get("confidence"),
            }
        )
    return result


def build_candidate_diff(
    baseline: list[dict[str, Any]], comparison: list[dict[str, Any]]
) -> dict[str, Any]:
    matcher = difflib.SequenceMatcher(
        a=[cue["text"].strip() for cue in baseline],
        b=[cue["text"].strip() for cue in comparison],
        autojunk=False,
    )
    differences = []
    timing_differences = []
    for diff_index, (tag, a0, a1, b0, b1) in enumerate(matcher.get_opcodes(), start=1):
        if tag == "equal":
            for offset in range(a1 - a0):
                left = baseline[a0 + offset]
                right = comparison[b0 + offset]
                start_delta = right["start_ms"] - left["start_ms"]
                end_delta = right["end_ms"] - left["end_ms"]
                if start_delta or end_delta:
                    timing_differences.append(
                        {
                            "change_id": f"timing-{len(timing_differences) + 1:03d}",
                            "baseline_candidate_id": left["candidate_id"],
                            "comparison_candidate_id": right["candidate_id"],
                            "text": left["text"],
                            "baseline_start_ms": left["start_ms"],
                            "comparison_start_ms": right["start_ms"],
                            "start_delta_ms": start_delta,
                            "baseline_end_ms": left["end_ms"],
                            "comparison_end_ms": right["end_ms"],
                            "end_delta_ms": end_delta,
                            "start_ms": min(left["start_ms"], right["start_ms"]),
                            "end_ms": max(left["end_ms"], right["end_ms"]),
                            "resolution": "pending_human_review",
                        }
                    )
            continue
        left = baseline[a0:a1]
        right = comparison[b0:b1]
        all_candidates = left + right
        if all_candidates:
            start_ms = min(cue["start_ms"] for cue in all_candidates)
            end_ms = max(cue["end_ms"] for cue in all_candidates)
        else:
            start_ms = end_ms = 0
        differences.append(
            {
                "change_id": f"diff-{diff_index:03d}",
                "operation": tag,
                "baseline_candidate_ids": [cue["candidate_id"] for cue in left],
                "comparison_candidate_ids": [cue["candidate_id"] for cue in right],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "resolution": "pending_human_review",
            }
        )
    return {
        "interpretation": (
            "Sequence alignment of OCR candidates only. Differences prioritize review; "
            "they are not ground truth and do not prove a missing or incorrect caption."
        ),
        "baseline_candidate_count": len(baseline),
        "comparison_candidate_count": len(comparison),
        "equal_text_blocks": sum(
            1
            for tag, *_ in matcher.get_opcodes()
            if tag == "equal"
        ),
        "differences": differences,
        "timing_differences": timing_differences,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_occurrences_csv(path: Path) -> None:
    write_csv(
        path,
        [],
        [
            "occurrence_id",
            "text_raw",
            "start_ms",
            "end_ms",
            "start_min_ms",
            "start_max_ms",
            "end_min_ms",
            "end_max_ms",
            "start_censored",
            "end_censored",
            "roi_or_line",
            "text_observability",
            "review_status",
            "reviewer",
            "reviewed_at_utc",
            "notes",
        ],
    )


def write_review_windows_csv(path: Path, duration_ms: int) -> None:
    rows = []
    start = 0
    while start < duration_ms:
        end = min(start + SWEEP_WINDOW_MS, duration_ms)
        rows.append(
            {
                "window_id": f"window-{start // SWEEP_WINDOW_MS + 1:03d}",
                "start_ms": start,
                "end_ms": end,
                "review_status": "pending",
                "caption_status": "pending",
                "occurrence_ids": "[]",
                "reviewer": "",
                "reviewed_at_utc": "",
                "notes": "",
            }
        )
        start = end
    write_csv(
        path,
        rows,
        [
            "window_id",
            "start_ms",
            "end_ms",
            "review_status",
            "caption_status",
            "occurrence_ids",
            "reviewer",
            "reviewed_at_utc",
            "notes",
        ],
    )


def write_candidate_review_csv(path: Path, candidates: list[dict[str, Any]]) -> None:
    fields = [
        "candidate_id",
        "run_id",
        "source",
        "candidate_text",
        "candidate_start_ms",
        "candidate_end_ms",
        "confidence",
        "occurrence_ids",
        "structure_status",
        "content_status",
        "timing_status",
        "reviewer",
        "reviewed_at_utc",
        "notes",
    ]
    rows = [
        {
            "candidate_id": cue["candidate_id"],
            "run_id": cue["run_id"],
            "source": cue["source"],
            "candidate_text": cue["text"],
            "candidate_start_ms": cue["start_ms"],
            "candidate_end_ms": cue["end_ms"],
            "confidence": cue["confidence"],
            "occurrence_ids": "[]",
            "structure_status": "",
        }
        for cue in candidates
    ]
    write_csv(path, rows, fields)


def render_blind_html(path: Path, video_path: Path, manifest: dict[str, Any]) -> None:
    video_uri = html.escape(video_path.resolve().as_uri(), quote=True)
    duration_ms = manifest["video_stream"]["duration_ms_from_first_frame"]
    document = f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OCR-00 blind subtitle inventory</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 18px;color:#18212b}}.notice{{background:#edf5ff;border-left:5px solid #3878bd;padding:14px 18px}}code{{background:#f1f3f5;padding:2px 5px}}li{{margin:.5em 0}}</style></head><body>
<h1>OCR-00 · Lượt ghi inventory phụ đề độc lập</h1>
<div class="notice"><strong>Chỉ mở video gốc trong lượt này.</strong> Không mở thư mục đối chiếu OCR cho tới khi inventory được khóa bằng validator.</div>
<p><a href="{video_uri}">Mở video gốc</a> · Thời lượng timeline: {duration_ms:,} ms.</p>
<h2>Quy tắc ghi nhận</h2>
<ul>
<li>Quét liên tục toàn bộ video theo từng cửa sổ trong <code>review_windows.csv</code>. Đánh dấu mọi cửa sổ đã xem, kể cả cửa sổ không có phụ đề.</li>
<li>Ghi mỗi lần hiển thị phụ đề vào một dòng riêng trong <code>occurrences.csv</code>; giữ nguyên chữ và xuống dòng. Một khối nhiều dòng là một occurrence. Nếu chữ biến mất rồi xuất hiện lại, ghi occurrence mới.</li>
<li>Chỉ ghi phụ đề/caption bị đè lên hình trong vùng OCR. Bỏ qua biển hiệu trong cảnh, giao diện, watermark/logo và chữ nền. Ghi lời thoại/SDH và lời bài hát nếu chúng được hiển thị như phụ đề.</li>
<li>Thời gian là millisecond nguyên tính từ frame video đầu tiên, dùng khoảng nửa mở [start, end). <code>start_min/max_ms</code> là khoảng onset có thể có; <code>end_min/max_ms</code> là khoảng kết thúc có thể có. Ghi điểm tốt nhất nằm trong khoảng đó. Nếu phụ đề đã hiện ở frame đầu, đặt start cùng hai bounds bằng 0 và đánh dấu censored; nếu còn hiện ở frame cuối, đặt end cùng bounds bằng duration và đánh dấu censored.</li>
<li>Nếu thấy phụ đề nhưng không đọc hết chữ, vẫn ghi occurrence, chọn mức quan sát phù hợp và không đoán phần bị che/mờ. Chỉ dùng trạng thái uncertain khi chưa thể kết luận có/không có occurrence.</li>
</ul>
<p>Ghi cùng <code>occurrence_id</code> vào mọi cửa sổ mà khoảng có thể có của occurrence giao với cửa sổ. Cửa sổ [a,b) giao occurrence [c,d) khi <code>c &lt; b</code> và <code>d &gt; a</code>.</p>
<p>Khi điền xong, chạy <code>backend/scripts/validate_subtitle_ocr_ground_truth.py freeze-blind --package &lt;thư-mục-gói&gt;</code>. Chỉ sau khi lệnh pass mới mở phần reconciliation và điền đối chiếu candidate.</p>
<p>Validator kiểm tra source video, inventory/cửa sổ, hash/provenance, text cặp khớp một-một và giao nhau thời gian. Nó không chứng minh người kiểm đã xem mọi frame hay chưa từng nhìn thấy candidate; split/merge vẫn cần adjudication thủ công.</p>
</body></html>"""
    path.write_text(document, encoding="utf-8")


def format_time(ms: int) -> str:
    return f"{ms / 1000:.3f}s"


def render_html(
    path: Path,
    video_path: Path,
    baseline: list[dict[str, Any]],
    comparison: list[dict[str, Any]],
    diff: dict[str, Any],
    frames: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    def image_cell(candidate: dict[str, Any], suffix: str) -> str:
        key = f"{candidate['candidate_id']}_{suffix}"
        sample = frames.get(key)
        if sample is None:
            return "<td>frame unavailable</td>"
        source = html.escape(sample["file"], quote=True)
        title = html.escape(
            f"requested={sample['requested_ms']}ms; PTS={sample['frame_pts_ms']}ms; "
            f"delta={sample['delta_ms']}ms"
        )
        label = html.escape(suffix.replace("_", " "))
        return (
            f'<td><a href="{source}" target="_blank" rel="noreferrer">'
            f'<img loading="lazy" src="{source}" alt="{label}" title="{title}"></a>'
            f'<small>{label}<br>{sample["frame_pts_ms"]:.1f}ms PTS '
            f'(Δ{sample["delta_ms"]:+.1f}ms)</small></td>'
        )

    main_rows = []
    for cue in baseline:
        cue_id = html.escape(cue["candidate_id"])
        confidence = (
            "" if cue["confidence"] is None else format(cue["confidence"], ".3f")
        )
        main_rows.append(
            f'<tr id="{cue_id}"><th>{cue_id}</th>'
            f'<td>{format_time(cue["start_ms"])}–{format_time(cue["end_ms"])}'
            f'<br><small>{cue["start_ms"]}–{cue["end_ms"]} ms</small></td>'
            f'<td class="text">{html.escape(cue["text"])}</td>'
            f"<td>{confidence}</td>"
            + "".join(
                image_cell(cue, name)
                for name in [
                    "start_minus_250",
                    "start_at",
                    "start_plus_250",
                    "middle",
                    "end_minus_250",
                    "end_at",
                    "end_plus_250",
                ]
            )
            + "</tr>"
        )

    diff_rows = []
    for change in diff["differences"]:
        baseline_cues = [
            cue for cue in baseline if cue["candidate_id"] in change["baseline_candidate_ids"]
        ]
        comparison_cues = [
            cue
            for cue in comparison
            if cue["candidate_id"] in change["comparison_candidate_ids"]
        ]
        left = "<br>".join(
            f'<a href="#{html.escape(cue["candidate_id"])}">'
            f'{html.escape(cue["candidate_id"])} {html.escape(cue["text"])} '
            f'({format_time(cue["start_ms"])})</a>'
            for cue in baseline_cues
        ) or "—"
        right = "<br>".join(
            f'{html.escape(cue["candidate_id"])} {html.escape(cue["text"])} '
            f'({format_time(cue["start_ms"])})'
            for cue in comparison_cues
        ) or "—"
        diff_rows.append(
            f'<tr><td>{html.escape(change["change_id"])}</td>'
            f'<td>{html.escape(change["operation"])}</td>'
            f'<td>{format_time(change["start_ms"])}–{format_time(change["end_ms"])}</td>'
            f'<td>{left}</td><td>{right}</td></tr>'
        )

    timing_rows = []
    for change in diff["timing_differences"]:
        baseline_cue = next(
            cue
            for cue in baseline
            if cue["candidate_id"] == change["baseline_candidate_id"]
        )
        comparison_cue = next(
            cue
            for cue in comparison
            if cue["candidate_id"] == change["comparison_candidate_id"]
        )
        timing_rows.append(
            f'<tr><td>{html.escape(change["change_id"])}</td>'
            f'<td><a href="#{html.escape(baseline_cue["candidate_id"])}">'
            f'{html.escape(baseline_cue["candidate_id"])}</a></td>'
            f'<td>{html.escape(comparison_cue["candidate_id"])}</td>'
            f'<td>{html.escape(change["text"])}</td>'
            f'<td>{change["start_delta_ms"]:+d} ms</td>'
            f'<td>{change["end_delta_ms"]:+d} ms</td>'
            f'<td>{format_time(change["start_ms"])}–{format_time(change["end_ms"])}</td></tr>'
        )

    comparison_samples = []
    changed_ids = {
        cue_id
        for change in diff["differences"]
        for cue_id in change["comparison_candidate_ids"]
    }
    changed_ids.update(
        change["comparison_candidate_id"] for change in diff["timing_differences"]
    )
    for cue in comparison:
        if cue["candidate_id"] not in changed_ids:
            continue
        cue_id = html.escape(cue["candidate_id"])
        comparison_samples.append(
            f'<tr><th>{cue_id}</th>'
            f'<td>{format_time(cue["start_ms"])}–{format_time(cue["end_ms"])}</td>'
            f'<td class="text">{html.escape(cue["text"])}</td>'
            + "".join(
                image_cell(cue, name) for name in ["start", "middle", "end"]
            )
            + "</tr>"
        )

    video_uri = html.escape(video_path.resolve().as_uri(), quote=True)
    diff_table = "".join(diff_rows) or '<tr><td colspan="5">No text-sequence differences.</td></tr>'
    timing_table = "".join(timing_rows) or '<tr><td colspan="7">No timing-only differences.</td></tr>'
    comparison_table = "".join(comparison_samples) or '<tr><td colspan="6">No comparison-only cues need extra stills.</td></tr>'
    document = f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OCR-00 human review</title>
<style>
body{{font:15px/1.45 system-ui,sans-serif;margin:24px;color:#17202a}} h1,h2{{margin:1.2em 0 .5em}}
.notice{{background:#fff4d6;border-left:5px solid #d89b16;padding:12px 16px;max-width:1100px}}
.meta{{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;max-width:1200px;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%;margin:12px 0 32px}} th,td{{border:1px solid #c8d0d8;padding:8px;vertical-align:top}}
thead{{position:sticky;top:0;background:#eef3f8;z-index:2}} th{{text-align:left}} .text{{min-width:220px;max-width:360px;overflow-wrap:anywhere}}
img{{width:180px;max-height:80px;object-fit:contain;background:#f1f3f5;display:block}} small{{color:#52606d;white-space:nowrap}}
.frames{{min-width:170px}} .diff{{max-width:1500px}} .muted{{color:#59636e}}
</style></head><body>
<h1>OCR-00: duyệt nhãn video gốc</h1>
<div class="notice"><strong>Đây là trang reconciliation; không dùng làm lượt lập ground truth độc lập.</strong> Chỉ mở sau khi <code>occurrences.csv</code> và <code>review_windows.csv</code> đã được freeze. Mọi đối chiếu là quyết định của reviewer, không lấy OCR làm đáp án.</div>
<p><a href="{video_uri}">Mở video gốc</a> · Ghi mapping và trạng thái lỗi trong <code>candidate_review.csv</code>.</p>
<p><code>occurrence_ids</code> là JSON array; <code>[]</code> nghĩa false positive. Chọn structure_status: matched, split, merge, split_merge, duplicate hoặc extra. Chọn content_status và timing_status độc lập; unreadable reference phải dùng not_scored cho nội dung. Với matched/merge, timing là within_bounds khi candidate start/end nằm trong khoảng nhãn tương ứng; biên censored được bỏ qua. Với split/duplicate/split_merge và false positive, timing_status là not_applicable vì không thể chấm biên của từng phần như toàn occurrence.</p>
<h2>Nguồn và runtime</h2><dl class="meta">{''.join(f'<dt>{html.escape(str(k))}</dt><dd>{html.escape(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else str(v))}</dd>' for k,v in manifest.items() if k not in {'generated_at_utc'})}</dl>
<h2>Khác biệt chuỗi 3 FPS / 1 FPS — vùng cần xem trước</h2>
<p class="muted">Sequence alignment là gợi ý tự động; thay đổi có thể do OCR, split/merge hoặc căn chỉnh lặp. Chỉ người xem video mới quyết định nhãn.</p>
<table class="diff"><thead><tr><th>ID</th><th>Opcode</th><th>Khoảng</th><th>Candidate 3 FPS</th><th>Candidate 1 FPS</th></tr></thead><tbody>{diff_table}</tbody></table>
<h2>Cùng text nhưng khác biên thời gian</h2>
<table class="diff"><thead><tr><th>ID</th><th>Candidate 3 FPS</th><th>Candidate 1 FPS</th><th>Text</th><th>Δ start</th><th>Δ end</th><th>Khoảng</th></tr></thead><tbody>{timing_table}</tbody></table>
<h2>Candidate 1 FPS trong vùng text hoặc timing khác biệt</h2>
<table><thead><tr><th>ID</th><th>Khoảng</th><th>OCR text</th><th>Đầu</th><th>Giữa</th><th>Cuối</th></tr></thead><tbody>{comparison_table}</tbody></table>
<h2>Toàn bộ 60 candidate 3 FPS</h2>
<p class="muted">Mỗi dòng có ảnh ROI tại start −250 ms, start, start +250 ms, midpoint, end −250 ms, end, end +250 ms. Các ảnh được chọn theo frame gần nhất bằng PTS.</p>
<table><thead><tr><th>ID</th><th>Biên OCR</th><th>OCR text</th><th>Conf.</th><th>Start −250</th><th>Start</th><th>Start +250</th><th>Giữa</th><th>End −250</th><th>End</th><th>End +250</th></tr></thead><tbody>{''.join(main_rows)}</tbody></table>
<footer class="muted">Package generated {html.escape(manifest['generated_at_utc'])}. Source SHA-256: {html.escape(manifest['video_sha256'])}.</footer>
</body></html>"""
    path.write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a blind-first OCR ground-truth package with a separate reconciliation folder."
    )
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    video_path = resolve_path(args.video)
    baseline_path = resolve_path(args.baseline)
    comparison_path = resolve_path(args.comparison)
    output_path = resolve_path(args.output)
    for required in (video_path, baseline_path, comparison_path):
        if not required.is_file():
            parser.error(f"File does not exist: {required}")
    if output_path.exists() and any(output_path.iterdir()):
        parser.error(f"Output directory is not empty: {output_path}")

    baseline_metrics = read_metrics(baseline_path)
    comparison_metrics = read_metrics(comparison_path)
    video_sha = sha256_file(video_path)
    for metrics_path, metrics in (
        (baseline_path, baseline_metrics),
        (comparison_path, comparison_metrics),
    ):
        expected_sha = str(metrics.get("sha256", "")).upper()
        if expected_sha and expected_sha != video_sha:
            parser.error(
                f"Video SHA-256 mismatch in {metrics_path}: metrics={expected_sha}, actual={video_sha}"
            )

    baseline_metrics_sha = sha256_file(baseline_path)
    comparison_metrics_sha = sha256_file(comparison_path)
    baseline_run_id = baseline_metrics_sha[:16]
    comparison_run_id = comparison_metrics_sha[:16]
    baseline = build_candidates(baseline_metrics, "fps3", baseline_run_id)
    comparison = build_candidates(comparison_metrics, "fps1", comparison_run_id)
    diff = build_candidate_diff(baseline, comparison)
    changed_comparison_ids = {
        cue_id
        for row in diff["differences"]
        for cue_id in row["comparison_candidate_ids"]
    }
    changed_comparison_ids.update(
        row["comparison_candidate_id"] for row in diff["timing_differences"]
    )

    output_path.mkdir(parents=True, exist_ok=True)
    reconciliation_dir = output_path / "reconciliation"
    reconciliation_dir.mkdir()
    frames_dir = reconciliation_dir / "frames"
    frames_dir.mkdir()
    roi = baseline_metrics["region_percent"]
    targets = frame_targets(baseline, source="fps3")
    targets += frame_targets(
        comparison, source="fps1", cue_ids=changed_comparison_ids
    )
    frames, stream_info = extract_review_frames(video_path, targets, roi, frames_dir)
    blind_stream_info = stream_info
    duration_ms = int(blind_stream_info["duration_ms_from_first_frame"])
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    manifest = {
        "generated_at_utc": timestamp,
        "video_path_at_generation": str(video_path.resolve()),
        "video_sha256": video_sha,
        "video_stream": blind_stream_info,
                "timeline_origin": "first decodable video frame; timestamps are rounded to integer milliseconds",
        "interval_convention": "half-open [start_ms, end_ms); coverage partitions [0, duration_ms)",
        "annotation_scope": (
            "Burned-in dialogue/SDH/lyrics rendered as subtitle overlays in the selected OCR region; "
            "exclude scene signs, UI, watermarks/logos, credits, and background text."
        ),
        "occurrence_identity": (
            "One continuous display of a subtitle text block. Multiline text remains one occurrence; "
            "a text change or disappearance followed by reappearance starts a new occurrence."
        ),
    }
    (output_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_occurrences_csv(output_path / "occurrences.csv")
    write_review_windows_csv(output_path / "review_windows.csv", duration_ms)
    render_blind_html(output_path / "index.html", video_path, manifest)

    candidate_snapshot = [
        {
            "candidate_id": cue["candidate_id"],
            "run_id": cue["run_id"],
            "source": cue["source"],
            "candidate_text": cue["text"],
            "candidate_start_ms": cue["start_ms"],
            "candidate_end_ms": cue["end_ms"],
            "confidence": cue["confidence"],
        }
        for cue in baseline + comparison
    ]
    candidate_snapshot_path = reconciliation_dir / "candidate-snapshot.json"
    candidate_snapshot_path.write_text(
        json.dumps(candidate_snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    baseline_metrics_snapshot_path = reconciliation_dir / "baseline-metrics.json"
    comparison_metrics_snapshot_path = reconciliation_dir / "comparison-metrics.json"
    shutil.copyfile(baseline_path, baseline_metrics_snapshot_path)
    shutil.copyfile(comparison_path, comparison_metrics_snapshot_path)
    reconciliation_manifest = {
        "generated_at_utc": timestamp,
        "video_sha256": video_sha,
        "source_video_sha256": video_sha,
        "baseline_metrics_path": str(baseline_path.resolve()),
        "baseline_metrics_sha256": baseline_metrics_sha,
        "baseline_metrics_snapshot": "baseline-metrics.json",
        "baseline_sample_fps": baseline_metrics.get("sample_fps"),
        "baseline_elapsed_seconds": baseline_metrics.get("elapsed_seconds"),
        "baseline_segment_count": len(baseline),
        "baseline_ocr_metrics": baseline_metrics.get("ocr_metrics", {}),
        "comparison_metrics_path": str(comparison_path.resolve()),
        "comparison_metrics_sha256": comparison_metrics_sha,
        "comparison_metrics_snapshot": "comparison-metrics.json",
        "comparison_sample_fps": comparison_metrics.get("sample_fps"),
        "comparison_elapsed_seconds": comparison_metrics.get("elapsed_seconds"),
        "comparison_segment_count": len(comparison),
        "comparison_diff_count": len(diff["differences"]),
        "timing_diff_count": len(diff["timing_differences"]),
        "baseline_runtime_version": baseline_metrics.get("runtime"),
        "baseline_stage_providers": baseline_metrics.get("stage_providers", {}),
        "baseline_run_id": baseline_run_id,
        "comparison_run_id": comparison_run_id,
        "video_stream": stream_info,
        "frame_still_count": len(frames),
        "frame_pts_index_path": "frame-index.json",
        "diff_interpretation": diff["interpretation"],
        "candidate_snapshot_sha256": sha256_file(candidate_snapshot_path),
        "expected_candidates": [
            {
                "candidate_id": cue["candidate_id"],
                "run_id": cue["run_id"],
                "source": cue["source"],
            }
            for cue in baseline + comparison
        ],
        "environment_snapshot": environment_snapshot(),
    }
    (reconciliation_dir / "reconciliation-manifest.json").write_text(
        json.dumps(reconciliation_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (reconciliation_dir / "cue-diff.json").write_text(
        json.dumps(diff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (reconciliation_dir / "frame-index.json").write_text(
        json.dumps(frames, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_candidate_review_csv(
        reconciliation_dir / "candidate_review.csv", baseline + comparison
    )

    readme = f"""# OCR-00 blind-first review package

The first pass is an independent visual inventory. Do not open `reconciliation/` until the blind inventory has been frozen.

- Open `index.html`; watch the entire linked source video.
- Fill `occurrences.csv` with every visible subtitle display, including multiline blocks, uncertainty bounds, censored video-edge events, and text readability.
- Complete every contiguous five-second window in `review_windows.csv`; use `[]` for a reviewed window with no visible subtitle. Link each occurrence in every window it may intersect.
- Freeze the independent pass with `backend\\.venv\\Scripts\\python.exe backend\\scripts\\validate_subtitle_ocr_ground_truth.py freeze-blind --package "{output_path.resolve()}"`.
- Only after freeze succeeds, open `reconciliation/index.html`; map candidates to occurrence IDs in `reconciliation/candidate_review.csv`. Select structure `matched`, `split`, `merge`, `split_merge`, `duplicate`, or `extra`; content `exact`, `text_error`, `not_scored`, or `false_positive`; timing `within_bounds`, `timing_error`, or `not_applicable`. The validator rehashes the source file, metrics and candidate snapshot; it checks `exact`/`text_error` against normalized source text for one-to-one `matched` rows and checks every mapping overlaps each linked visual occurrence. Matched/merge boundary timing is checked against reviewer intervals. Split/duplicate/split_merge timing boundaries remain unscored and require `not_applicable`; empty ID arrays also require `extra/false_positive`. Split/merge text remains human-adjudicated. Then run the validator with `validate`.

Video SHA-256: `{video_sha}`

Duration: {duration_ms} ms

Blind inventory: pending; no OCR text/count/timing is included on the first-pass page.
"""
    (output_path / "README.md").write_text(readme, encoding="utf-8")
    quality_review = f"""# OCR-00 manual quality review

**Status:** Pending blind inventory. No OCR candidate is treated as ground truth.

## Blind inventory

- Video SHA-256: `{video_sha}`
- Duration: {duration_ms} ms from first decodable video frame
- Reviewer:
- Review date (UTC):
- Independent second reviewer / adjudicator:
- Blind inventory frozen: pending
- Occurrence count:
- Unreadable/partially unreadable occurrence count:
- All windows reviewed: pending

## Candidate reconciliation (complete only after blind freeze)

- Candidate review validated: pending
- False-positive candidates by run:
- Missed occurrences by run:
- Split occurrences by run:
- Merged candidates by run:
- Text errors (numbers, negation, names, other):
- Timing errors outside visible-boundary intervals:
- Unresolved cases:

## Conclusion

Pending. The validator checks source media, structure, hashes/provenance, simple one-to-one text, temporal overlap, and boundary intervals where comparable. A human reviewer must still attest to full viewing and adjudicate split/merge text and structure. Do not use these labels to tune OCR until blind inventory and candidate reconciliation are both complete.
"""
    (output_path / "quality-review.md").write_text(quality_review, encoding="utf-8")
    render_html(
        reconciliation_dir / "index.html",
        video_path,
        baseline,
        comparison,
        diff,
        frames,
        reconciliation_manifest,
    )

    print(
        json.dumps(
            {
                "output": str(output_path.resolve()),
                "video_sha256": video_sha,
                "duration_ms": duration_ms,
                "blind_inventory": "pending",
                "reconciliation_output": "separate; do not open until blind inventory is frozen",
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
