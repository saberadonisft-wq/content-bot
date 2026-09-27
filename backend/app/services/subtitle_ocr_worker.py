"""One-job CUDA OCR worker, kept separate from the backend's CPU ORT module."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

GPU_ORT_VERSION = "1.26.0"
_DLL_HANDLES: list[Any] = []


class OcrWorkerFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OcrWorkerCanceled(Exception):
    pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_gpu_runtime(profile_path: Path):
    profile = profile_path.resolve()
    if not profile.is_absolute() or not profile.is_dir():
        raise OcrWorkerFailure(
            "ocr_runtime_unavailable", "Chưa cài runtime OCR GPU riêng."
        )

    # This worker is a fresh process. Put the GPU profile ahead of the backend
    # CPU site-packages before importing ONNX Runtime or RapidOCR.
    sys.path.insert(0, str(profile))
    dll_root = profile / "nvidia"
    dll_dirs = sorted(path for path in dll_root.glob("*/bin") if path.is_dir())
    if not dll_dirs:
        raise OcrWorkerFailure(
            "ocr_cuda_init_failed", "Runtime OCR GPU thiếu thư viện CUDA đi kèm."
        )
    if hasattr(os, "add_dll_directory"):
        for directory in dll_dirs:
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
    os.environ["PATH"] = os.pathsep.join(
        [*(str(path) for path in dll_dirs), os.environ.get("PATH", "")]
    )

    try:
        import flatbuffers
        import google.protobuf
        import numpy
        import onnxruntime as ort
        import packaging
    except Exception as exc:
        raise OcrWorkerFailure(
            "ocr_cuda_init_failed", "Không nạp được ONNX Runtime GPU."
        ) from exc

    try:
        if not Path(ort.__file__).resolve().is_relative_to(profile):
            raise OcrWorkerFailure(
                "ocr_provider_mismatch",
                "Worker không nạp ONNX Runtime từ profile GPU đã cấu hình.",
            )
        if ort.__version__ != GPU_ORT_VERSION:
            raise OcrWorkerFailure(
                "ocr_provider_mismatch",
                f"Profile OCR GPU cần ONNX Runtime {GPU_ORT_VERSION}.",
            )
        for name, module in (
            ("NumPy", numpy),
            ("flatbuffers", flatbuffers),
            ("packaging", packaging),
            ("protobuf", google.protobuf),
        ):
            if Path(module.__file__).resolve().is_relative_to(profile):
                raise OcrWorkerFailure(
                    "ocr_provider_mismatch",
                    f"Profile OCR GPU không được thay thế dependency {name} của backend.",
                )
        preload = getattr(ort, "preload_dlls", None)
        if preload:
            preload()
        providers = list(ort.get_available_providers())
    except OcrWorkerFailure:
        raise
    except Exception as exc:
        raise OcrWorkerFailure(
            "ocr_cuda_init_failed", "Không khởi tạo được CUDA cho ONNX Runtime."
        ) from exc
    if "CUDAExecutionProvider" not in providers:
        raise OcrWorkerFailure(
            "ocr_provider_mismatch", "ONNX Runtime hiện không có CUDA provider."
        )
    return ort, providers, [str(path) for path in dll_dirs]


def _classify_failure(exc: Exception, phase: str) -> OcrWorkerFailure:
    message = str(exc).lower()
    if any(
        marker in message
        for marker in (
            "out of memory",
            "resourceexhausted",
            "failed to allocate memory",
            "cuda_error_out_of_memory",
        )
    ):
        return OcrWorkerFailure("ocr_out_of_memory", "GPU không đủ VRAM để chạy OCR.")
    if "provider" in message or "cudaexecutionprovider" in message:
        return OcrWorkerFailure(
            "ocr_provider_mismatch", "Một stage OCR không chạy trên CUDA như yêu cầu."
        )
    if phase == "gpu_wait":
        return OcrWorkerFailure("ocr_gpu_wait_timeout", "Hết thời gian chờ GPU.")
    if phase in {"preflight", "model_init"}:
        return OcrWorkerFailure(
            "ocr_cuda_init_failed", "Không khởi tạo được mô hình OCR trên CUDA."
        )
    return OcrWorkerFailure("ocr_worker_failed", "Worker OCR GPU gặp lỗi khi xử lý video.")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Cần truyền thư mục request của worker OCR.")
    root = Path(sys.argv[1]).resolve()
    heartbeat = root / "heartbeat"

    def watch_heartbeat() -> None:
        while True:
            try:
                expired = time.time() - heartbeat.stat().st_mtime > 30
            except OSError:
                expired = True
            if expired:
                if os.name != "nt":
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(75)
            time.sleep(1)

    threading.Thread(target=watch_heartbeat, daemon=True).start()
    while not (root / "ready").exists():
        time.sleep(0.05)

    class Context:
        def raise_if_canceled(self) -> None:
            if (root / "cancel").exists():
                raise OcrWorkerCanceled("Đã hủy trích xuất OCR.")

        def update(self, progress: int, phase: str, message: str) -> None:
            self.raise_if_canceled()
            # The supervisor polls this file while the worker updates it.
            # Replacing an open destination can fail with WinError 5 on
            # Windows, so progress is a best-effort in-place status write;
            # the parent already ignores partially written JSON and retries.
            try:
                (root / "progress.json").write_text(
                    json.dumps(
                        {"progress": progress, "phase": phase, "message": message},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
            except OSError:
                return

    phase = "preflight"
    try:
        payload = json.loads((root / "request.json").read_text(encoding="utf-8"))
        context = Context()
        context.raise_if_canceled()
        ort, available_providers, dll_dirs = _load_gpu_runtime(
            Path(payload["gpu_site_packages"])
        )
        from ..schemas import SubtitleOcrRegion
        from . import subtitle_ocr
        phase = "model_init"
        context.update(5, "loading_model", "Đang nạp mô hình OCR lên GPU...")
        model_started = time.perf_counter()
        engine, stage_providers = subtitle_ocr._create_ocr_engine(use_cuda=True)
        model_load_seconds = time.perf_counter() - model_started
        subtitle_ocr._RAPID_OCR_INSTANCE = engine

        phase = "extracting"
        context.update(10, "scanning_frames", "Đang phân tích khung hình video...")
        result = subtitle_ocr.extract_subtitles_ocr(
            Path(payload["video_path"]),
            SubtitleOcrRegion.model_validate(payload["region"]),
            payload["media"],
            source_language=payload.get("source_language"),
            sample_fps=float(payload["sample_fps"]),
            min_duration_ms=int(payload["min_duration_ms"]),
            max_gap_ms=int(payload["max_gap_ms"]),
            auto_probe=bool(payload["auto_probe"]),
            prefetch_frames=bool(payload.get("prefetch_frames", True)),
            glyph_cache=bool(payload.get("glyph_cache", True)),
            acceleration=subtitle_ocr.OcrAcceleration.model_validate(payload.get("acceleration") or {}),
            cache_dir=Path(payload["cache_dir"]),
            context=context,
        )
        context.raise_if_canceled()
        result["runtime"] = {
            "requested_device": "cuda",
            "effective_device": "cuda",
            "onnxruntime_version": ort.__version__,
            "available_providers": available_providers,
            "stage_providers": stage_providers,
            "model_load_seconds": round(model_load_seconds, 4),
            "runtime_profile": "ocr_gpu_overlay",
            "cuda_dll_directory_count": len(dll_dirs),
        }
        _atomic_json(root / "result.json", result)
    except OcrWorkerCanceled as exc:
        _atomic_json(
            root / "error.json", {"canceled": True, "message": str(exc)}
        )
    except OcrWorkerFailure as exc:
        _atomic_json(root / "error.json", {"code": exc.code, "message": str(exc)})
        raise SystemExit(1) from exc
    except Exception as exc:
        failure = _classify_failure(exc, phase)
        _atomic_json(
            root / "error.json", {"code": failure.code, "message": str(failure)}
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
