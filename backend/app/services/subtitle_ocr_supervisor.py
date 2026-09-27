"""Select CPU or isolated CUDA OCR and own the one-job GPU worker lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .gemini_media import atomic_json
from .model_resources import gpu_model_slot
from .owned_process import OwnedProcess
from .process_metrics import nvidia_memory_snapshot, process_rss_bytes
from .subtitle_jobs import SubtitleJobCanceled
from .subtitle_ocr import (
    OcrAcceleration,
    SubtitleOcrCanceled,
    SubtitleOcrError,
    extract_subtitles_ocr,
)

_GPU_FAILURES_ELIGIBLE_FOR_CPU_RETRY = {
    "ocr_runtime_unavailable",
    "ocr_cuda_init_failed",
    "ocr_provider_mismatch",
    "ocr_out_of_memory",
    "ocr_gpu_wait_timeout",
    "ocr_worker_timeout",
    "ocr_worker_failed",
}


def subtitle_ocr_runtime_signature(device_policy: str, gpu_site_packages: Path) -> str:
    """Change job dedupe whenever OCR can select a different runtime profile."""
    if device_policy == "cpu":
        return f"cpu-ort-{_cpu_onnxruntime_version()}"

    profile = Path(gpu_site_packages).resolve()
    if not profile.is_dir():
        return f"{device_policy}-gpu-runtime-missing"

    manifest = profile / "runtime-manifest.json"
    try:
        fingerprint = hashlib.sha256(manifest.read_bytes()).hexdigest()
        return f"{device_policy}-gpu-manifest-{fingerprint}"
    except OSError:
        # Older/manual profiles have no installer manifest. Include the ORT
        # binary metadata so replacing the profile still invalidates dedupe.
        runtime_binary = profile / "onnxruntime" / "capi" / "onnxruntime_pybind11_state.pyd"
        try:
            stat = runtime_binary.stat()
            return f"{device_policy}-gpu-unmanaged-{stat.st_size}-{stat.st_mtime_ns}"
        except OSError:
            return f"{device_policy}-gpu-unmanaged"


def run_subtitle_ocr_job(
    video_path: Path,
    region: Any,
    media: dict[str, Any],
    *,
    device_policy: str,
    gpu_site_packages: Path,
    worker_timeout_seconds: int,
    context: Any,
    cache_dir: Path,
    source_language: str | None,
    sample_fps: float,
    min_duration_ms: int,
    max_gap_ms: int,
    auto_probe: bool,
    prefetch_frames: bool = True,
    glyph_cache: bool = True,
    acceleration: OcrAcceleration | None = None,
) -> dict[str, Any]:
    acceleration = OcrAcceleration.model_validate(acceleration or {})
    if device_policy not in {"auto", "cpu", "cuda"}:
        raise SubtitleOcrError("OCR device policy không hợp lệ.")
    if device_policy == "cpu":
        return _run_cpu(
            video_path,
            region,
            media,
            context=context,
            cache_dir=cache_dir,
            source_language=source_language,
            sample_fps=sample_fps,
            min_duration_ms=min_duration_ms,
            max_gap_ms=max_gap_ms,
            auto_probe=auto_probe,
            prefetch_frames=prefetch_frames,
            glyph_cache=glyph_cache,
            requested_device="cpu",
            acceleration=acceleration,
        )

    profile = Path(gpu_site_packages).resolve()
    if not profile.is_dir():
        if device_policy == "cuda":
            raise SubtitleOcrError(
                "Runtime OCR GPU chưa được cài; chạy scripts/install_subtitle_ocr_gpu_runtime.ps1."
            )
        return _run_cpu(
            video_path,
            region,
            media,
            context=context,
            cache_dir=cache_dir,
            source_language=source_language,
            sample_fps=sample_fps,
            min_duration_ms=min_duration_ms,
            max_gap_ms=max_gap_ms,
            auto_probe=auto_probe,
            prefetch_frames=prefetch_frames,
            glyph_cache=glyph_cache,
            requested_device="auto",
            acceleration=acceleration,
            fallback_reason="ocr_runtime_unavailable",
        )

    try:
        if context:
            context.update(2, "waiting_gpu", "Đang chờ lượt sử dụng GPU...")
        slot_wait_started = time.monotonic()
        try:
            with gpu_model_slot(
                "cuda",
                check_canceled=context.raise_if_canceled if context else lambda: None,
                timeout_seconds=worker_timeout_seconds,
            ):
                slot_wait_seconds = time.monotonic() - slot_wait_started
                result = _run_gpu_worker(
                    video_path,
                    region,
                    media,
                    gpu_site_packages=profile,
                    worker_timeout_seconds=worker_timeout_seconds,
                    context=context,
                    cache_dir=cache_dir,
                    source_language=source_language,
                    sample_fps=sample_fps,
                    min_duration_ms=min_duration_ms,
                    max_gap_ms=max_gap_ms,
                    auto_probe=auto_probe,
                    prefetch_frames=prefetch_frames,
                    glyph_cache=glyph_cache,
                    requested_device=device_policy,
                    acceleration=acceleration,
                )
        except TimeoutError as exc:
            raise SubtitleOcrError(
                "Hết thời gian chờ GPU.", code="ocr_gpu_wait_timeout"
            ) from exc
        result.setdefault("runtime", {})["gpu_slot_wait_seconds"] = round(
            slot_wait_seconds, 4
        )
        return result
    except SubtitleJobCanceled:
        raise
    except Exception as exc:
        failure = (
            exc
            if isinstance(exc, SubtitleOcrError)
            else SubtitleOcrError(
                "Worker OCR GPU kết thúc bất thường.", code="ocr_worker_failed"
            )
        )
        if (
            device_policy == "cuda"
            or _failure_code(failure) not in _GPU_FAILURES_ELIGIBLE_FOR_CPU_RETRY
        ):
            if failure is exc:
                raise
            raise failure from exc
        reason = _failure_code(failure) or "ocr_worker_failed"
        return _run_cpu(
            video_path,
            region,
            media,
            context=context,
            cache_dir=cache_dir,
            source_language=source_language,
            sample_fps=sample_fps,
            min_duration_ms=min_duration_ms,
            max_gap_ms=max_gap_ms,
            auto_probe=auto_probe,
            prefetch_frames=prefetch_frames,
            glyph_cache=glyph_cache,
            requested_device="auto",
            acceleration=acceleration,
            fallback_reason=reason,
        )


def _failure_code(error: Exception) -> str | None:
    return getattr(error, "code", None)


def _run_cpu(
    video_path: Path,
    region: Any,
    media: dict[str, Any],
    *,
    context: Any,
    cache_dir: Path,
    source_language: str | None,
    sample_fps: float,
    min_duration_ms: int,
    max_gap_ms: int,
    auto_probe: bool,
    requested_device: str,
    prefetch_frames: bool = True,
    glyph_cache: bool = True,
    acceleration: OcrAcceleration | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    if context:
        context.raise_if_canceled()
    try:
        result = extract_subtitles_ocr(
            video_path,
            region,
            media,
            source_language=source_language,
            sample_fps=sample_fps,
            min_duration_ms=min_duration_ms,
            max_gap_ms=max_gap_ms,
            auto_probe=auto_probe,
            prefetch_frames=prefetch_frames,
            glyph_cache=glyph_cache,
            acceleration=acceleration,
            cache_dir=cache_dir,
            context=context,
        )
    except SubtitleOcrCanceled as exc:
        raise SubtitleJobCanceled(str(exc)) from exc
    result["runtime"] = {
        "requested_device": requested_device,
        "effective_device": "cpu",
        "onnxruntime_version": _cpu_onnxruntime_version(),
    }
    if fallback_reason:
        result["runtime"]["fallback_reason"] = fallback_reason
        result.setdefault("warnings", []).append(
            {
                "code": "ocr_cpu_fallback",
                "message": "GPU OCR không sẵn sàng; toàn bộ video đã được chạy lại bằng CPU.",
            }
        )
    return result


def _cpu_onnxruntime_version() -> str:
    try:
        import onnxruntime as ort

        return ort.__version__
    except Exception:
        return "unavailable"


def _run_gpu_worker(
    video_path: Path,
    region: Any,
    media: dict[str, Any],
    *,
    gpu_site_packages: Path,
    worker_timeout_seconds: int,
    context: Any,
    cache_dir: Path,
    source_language: str | None,
    sample_fps: float,
    min_duration_ms: int,
    max_gap_ms: int,
    auto_probe: bool,
    prefetch_frames: bool,
    glyph_cache: bool,
    requested_device: str,
    acceleration: OcrAcceleration | None = None,
) -> dict[str, Any]:
    if context:
        context.raise_if_canceled()
    backend_root = Path(__file__).resolve().parents[2]
    payload = {
        "video_path": str(Path(video_path).resolve()),
        "region": region.model_dump(mode="json")
        if hasattr(region, "model_dump")
        else region,
        "media": media,
        "source_language": source_language,
        "sample_fps": sample_fps,
        "min_duration_ms": min_duration_ms,
        "max_gap_ms": max_gap_ms,
        "auto_probe": auto_probe,
        "prefetch_frames": prefetch_frames,
        "glyph_cache": glyph_cache,
        "acceleration": (acceleration or OcrAcceleration()).model_dump(),
        "cache_dir": str(Path(cache_dir).resolve()),
        "gpu_site_packages": str(gpu_site_packages.resolve()),
    }
    with tempfile.TemporaryDirectory(prefix="content-bot-ocr-") as directory:
        root = Path(directory)
        atomic_json(root / "request.json", payload)
        (root / "heartbeat").touch()
        with (root / "worker.log").open("w", encoding="utf-8") as log:
            child = OwnedProcess(
                [
                    sys.executable,
                    "-m",
                    "app.services.subtitle_ocr_worker",
                    str(root),
                ],
                cwd=backend_root,
                stdout=log,
                stderr=log,
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "OMP_NUM_THREADS": "4",
                    "MKL_NUM_THREADS": "4",
                    "OPENBLAS_NUM_THREADS": "4",
                },
            )
            previous = None
            peak_rss_bytes = 0
            peak_gpu_memory: dict[int, int] = {}
            started = time.monotonic()
            last_gpu_sample = started
            last_gpu_phase: str | None = None
            stage_deadline = started + worker_timeout_seconds
            try:
                # The worker waits for process-tree ownership before importing CUDA.
                (root / "ready").touch()
                while child.process.poll() is None:
                    now = time.monotonic()
                    rss = process_rss_bytes(child.process.pid)
                    if rss is not None:
                        peak_rss_bytes = max(peak_rss_bytes, rss)
                    if context:
                        context.raise_if_canceled()
                    (root / "heartbeat").touch()
                    progress_path = root / "progress.json"
                    progress = None
                    if progress_path.exists():
                        try:
                            progress = json.loads(
                                progress_path.read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            progress = None
                        if progress is not None and progress != previous:
                            previous = progress
                            stage_deadline = time.monotonic() + worker_timeout_seconds
                            if context:
                                context.update(
                                    progress["progress"],
                                    progress["phase"],
                                    progress["message"],
                                )
                    progress_phase = progress.get("phase") if progress else None
                    # nvidia-smi is a separate process. Avoid launching it at
                    # 2 Hz; sample once after model load and then at a bounded
                    # 30-second cadence for long jobs.
                    should_sample_gpu = (
                        progress_phase in {"scanning_frames", "processing_ocr"}
                        and last_gpu_phase not in {"scanning_frames", "processing_ocr"}
                    ) or now - last_gpu_sample >= 30.0
                    if should_sample_gpu:
                        for gpu in nvidia_memory_snapshot():
                            peak_gpu_memory[gpu["index"]] = max(
                                peak_gpu_memory.get(gpu["index"], 0),
                                gpu["used_bytes"],
                            )
                        last_gpu_sample = now
                    if progress_phase:
                        last_gpu_phase = progress_phase
                    if now > stage_deadline:
                        timed_out_while_waiting = progress_phase == "waiting_gpu"
                        timeout_code = (
                            "ocr_gpu_wait_timeout"
                            if timed_out_while_waiting
                            else "ocr_worker_timeout"
                        )
                        raise SubtitleOcrError(
                            "Worker OCR GPU đã quá thời gian chờ; tiến trình đã được thu hồi."
                            if timed_out_while_waiting
                            else "Worker OCR GPU xử lý quá thời gian; tiến trình đã được thu hồi.",
                            code=timeout_code,
                        )
                    time.sleep(0.1)
                if context:
                    context.raise_if_canceled()
                if (root / "error.json").exists():
                    error = json.loads((root / "error.json").read_text(encoding="utf-8"))
                    if error.get("canceled"):
                        raise SubtitleJobCanceled(error.get("message", "Đã hủy OCR."))
                    code = str(error.get("code") or "ocr_worker_failed")
                    if code == "ocr_gpu_wait_timeout":
                        message = "Hết thời gian chờ GPU."
                    elif code == "ocr_worker_timeout":
                        message = "OCR GPU xử lý quá thời gian cho phép."
                    else:
                        message = str(error.get("message") or "Worker OCR GPU thất bại.")
                    raise SubtitleOcrError(message, code=code)
                if child.process.returncode or not (root / "result.json").exists():
                    raise SubtitleOcrError(
                        "Worker OCR GPU kết thúc bất thường.", code="ocr_worker_failed"
                    )
                result = json.loads((root / "result.json").read_text(encoding="utf-8"))
                result.setdefault("runtime", {})["requested_device"] = requested_device
                runtime_metrics = {
                    "worker_wall_seconds": round(time.monotonic() - started, 3),
                }
                if peak_rss_bytes:
                    runtime_metrics.update(
                        {
                            "worker_peak_rss_bytes": peak_rss_bytes,
                            "worker_peak_rss_source": "GetProcessMemoryInfo"
                            if os.name == "nt"
                            else "/proc/<pid>/status",
                        }
                    )
                if peak_gpu_memory:
                    runtime_metrics.update(
                        {
                            "gpu_peak_memory_bytes": peak_gpu_memory,
                            "gpu_peak_memory_source": "nvidia-smi; whole-device usage",
                        }
                    )
                result["runtime_metrics"] = runtime_metrics
                return result
            except OSError as exc:
                raise SubtitleOcrError(
                    "Không thể khởi chạy worker OCR GPU.", code="ocr_worker_failed"
                ) from exc
            finally:
                child.close()
