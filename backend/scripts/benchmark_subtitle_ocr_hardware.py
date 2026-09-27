"""Compare OCR CPU/GPU execution providers on the same real video and ROI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
_GPU_DLL_HANDLES: list[Any] = []


class _Progress:
    def __init__(self) -> None:
        self.last_bucket = -1
        self.last_phase = ""

    def update(self, percent: int, phase: str, message: str) -> None:
        bucket = percent // 5
        if bucket != self.last_bucket or phase != self.last_phase:
            print(f"[{percent:3d}%] {phase}: {message}", flush=True)
            self.last_bucket, self.last_phase = bucket, phase

    def raise_if_canceled(self) -> None:
        return


class _TimedStage:
    def __init__(self, stage: Any, name: str, timings: dict[str, float]) -> None:
        self.stage = stage
        self.name = name
        self.timings = timings
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        started = time.perf_counter()
        value = self.stage(*args, **kwargs)
        self.timings[self.name] = self.timings.get(self.name, 0.0) + (
            time.perf_counter() - started
        )
        return value


def _engine(mode: str, ocr_module: Any) -> tuple[Any, dict[str, list[str]]]:
    import onnxruntime as ort
    from rapidocr_onnxruntime import RapidOCR

    if mode != "cpu":
        nvidia_root = Path(ort.__file__).resolve().parent.parent / "nvidia"
        dll_dirs = list(nvidia_root.glob("*/bin")) if nvidia_root.is_dir() else []
        if dll_dirs:
            os.environ["PATH"] = (
                os.pathsep.join(str(path) for path in dll_dirs)
                + os.pathsep
                + os.environ.get("PATH", "")
            )
        if nvidia_root.is_dir() and hasattr(os, "add_dll_directory"):
            for dll_dir in dll_dirs:
                _GPU_DLL_HANDLES.append(os.add_dll_directory(str(dll_dir)))
        preload = getattr(ort, "preload_dlls", None)
        if preload:
            preload()
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError(
                "ONNX Runtime không có CUDAExecutionProvider; dừng để tránh báo nhầm GPU khi tự rơi về CPU."
            )

    enabled = mode == "gpu"
    if mode not in {"cpu", "det_gpu", "gpu"}:
        raise ValueError(f"Chế độ không hợp lệ: {mode}")
    engine_args = {
        "width_height_ratio": -1,
        "min_height": 0,
        "text_score": 0.0,
        "det_limit_type": "max",
        "det_limit_side_len": 1280,
        "det_model_path": None,
    }
    if mode in {"det_gpu", "gpu"}:
        engine_args["det_use_cuda"] = True
    if mode == "det_gpu":
        engine_args.update(rec_use_cuda=False, rec_model_path=None)
    if enabled:
        # RapidOCR 1.2.3 requires each model_path key with stage overrides.
        engine_args.update(
            cls_use_cuda=True,
            cls_model_path=None,
            rec_use_cuda=True,
            rec_model_path=None,
        )
    if mode == "gpu":
        # RapidOCR 1.2.3 leaves cls_*/rec_* prefixes on use_cuda, so those
        # model sessions silently stay on CPU. Normalize only for this run.
        from rapidocr_onnxruntime.utils import UpdateParameters

        originals: dict[str, Callable[..., Any]] = {}
        for stage in ("cls", "rec"):
            method_name = f"update_{stage}_params"
            original = getattr(UpdateParameters, method_name)
            originals[method_name] = original

            def make_normalizer(
                prefix: str, callback: Callable[..., Any]
            ) -> Callable[..., Any]:
                def normalized(
                    self: Any, config: dict[str, Any], values: dict[str, Any]
                ) -> Any:
                    normalized_values = {
                        key.removeprefix(f"{prefix}_"): value
                        for key, value in values.items()
                    }
                    return callback(self, config, normalized_values)

                return normalized

            setattr(UpdateParameters, method_name, make_normalizer(stage, original))
        try:
            engine = RapidOCR(**engine_args)
        finally:
            for method_name, original in originals.items():
                setattr(UpdateParameters, method_name, original)
    else:
        engine = RapidOCR(**engine_args)
    providers = {
        "detector": engine.text_detector.infer.session.get_providers(),
        "classifier": engine.text_cls.infer.session.get_providers(),
        "recognizer": engine.text_recognizer.session.session.get_providers(),
    }
    if mode in {"det_gpu", "gpu"}:
        engine.text_detector.infer.session.disable_fallback()
    if mode == "gpu":
        engine.text_cls.infer.session.disable_fallback()
        engine.text_recognizer.session.session.disable_fallback()
    if mode == "det_gpu" and any(
        not stages or stages[0] != "CUDAExecutionProvider"
        for name, stages in providers.items()
        if name == "detector"
    ):
        raise RuntimeError(f"Detector không chạy CUDA như yêu cầu: {providers}")
    if mode == "gpu" and any(
        not stages or stages[0] != "CUDAExecutionProvider"
        for stages in providers.values()
    ):
        raise RuntimeError(f"Có model không chạy CUDA như yêu cầu: {providers}")
    return engine, providers


def run_mode(args: argparse.Namespace) -> Path:
    import av
    import onnxruntime as ort

    import app.services.subtitle_ocr as ocr_module
    from app.schemas import SubtitleOcrRegion

    video = args.video.resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    with video.open("rb") as source:
        fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        metadata = {
            "duration_ms": round(float(container.duration / av.time_base) * 1000),
            "fingerprint": fingerprint,
            "file_size_bytes": video.stat().st_size,
            "width": stream.width,
            "height": stream.height,
            "rotation": 0,
        }
    region = SubtitleOcrRegion(x=args.x, y=args.y, width=args.width, height=args.height)

    # Keep app extraction/decoding/temporal logic identical; vary only RapidOCR providers.
    model_started = time.perf_counter()
    engine, stage_providers = _engine(args.mode, ocr_module)
    model_load_seconds = time.perf_counter() - model_started
    stage_seconds: dict[str, float] = {}
    engine.text_detector = _TimedStage(engine.text_detector, "detector", stage_seconds)
    engine.text_cls = _TimedStage(engine.text_cls, "classifier", stage_seconds)
    engine.text_recognizer = _TimedStage(engine.text_recognizer, "recognizer", stage_seconds)
    ocr_module._RAPID_OCR_INSTANCE = engine
    started = time.perf_counter()
    result = ocr_module.extract_subtitles_ocr(
        video,
        region,
        metadata,
        source_language="zh",
        sample_fps=args.fps,
        min_duration_ms=300,
        auto_probe=False,
        cache_dir=None,
        context=_Progress(),
    )
    elapsed = time.perf_counter() - started
    document = result["document"]
    stage_call_counts = {
        "det_calls": engine.text_detector.call_count,
        "cls_calls": engine.text_cls.call_count,
        "rec_calls": engine.text_recognizer.call_count,
    }
    ocr_metrics = {**result["metrics"], **stage_call_counts}
    summary = {
        "mode": args.mode,
        "video": str(video),
        "sha256": fingerprint,
        "runtime": ort.__version__,
        "available_providers": ort.get_available_providers(),
        "stage_providers": stage_providers,
        "region_percent": {"x": args.x, "y": args.y, "width": args.width, "height": args.height},
        "sample_fps": args.fps,
        "elapsed_seconds": round(elapsed, 3),
        "model_load_seconds": round(model_load_seconds, 3),
        "stage_elapsed_seconds": {
            key: round(value, 3) for key, value in stage_seconds.items()
        },
        "ocr_metrics": ocr_metrics,
        "timings_seconds": result["timings_seconds"],
        "segment_count": len(document["segments"]),
        "segments": [
            {key: cue[key] for key in ("start_ms", "end_ms", "text", "confidence")}
            for cue in document["segments"]
        ],
    }
    out_file = args.output / f"{args.mode}.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": args.mode,
                "elapsed_seconds": summary["elapsed_seconds"],
                "providers": stage_providers,
                "segments": summary["segment_count"],
                "metrics": summary["ocr_metrics"],
                "timings_seconds": summary["timings_seconds"],
                "report": str(out_file),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return out_file


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--mode", choices=("cpu", "det_gpu", "gpu", "all"), default="all")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts/subtitle-remediation/phase7-ocr/hardware")
    parser.add_argument("--fps", type=float, default=3.0)
    parser.add_argument("--x", type=float, default=10.0)
    parser.add_argument("--y", type=float, default=86.0)
    parser.add_argument("--width", type=float, default=78.0)
    parser.add_argument("--height", type=float, default=14.0)
    args = parser.parse_args()

    if args.mode == "all":
        for mode in ("cpu", "det_gpu", "gpu"):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                str(args.video.resolve()),
                "--mode",
                mode,
                "--output",
                str(args.output.resolve()),
                "--fps",
                str(args.fps),
                "--x",
                str(args.x),
                "--y",
                str(args.y),
                "--width",
                str(args.width),
                "--height",
                str(args.height),
            ]
            completed = subprocess.run(
                command, cwd=REPO_ROOT, env=os.environ.copy(), check=False
            )
            if completed.returncode:
                raise SystemExit(completed.returncode)
        return

    run_mode(args)


if __name__ == "__main__":
    main()
