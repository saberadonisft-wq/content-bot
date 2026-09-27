"""Opt-in smoke against a real local video and CUDA runtime.

Set CONTENT_BOT_OCR_GPU_SMOKE_VIDEO to a path before running this test. It is
intentionally skipped in CPU-only CI and validates the supervised worker path,
not merely ONNX Runtime provider discovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import pytest

from app.config import settings
from app.schemas import SubtitleOcrRegion
from app.services.gemini_media import atomic_json
from app.services.media_probe import probe_media
from app.services.subtitle_ocr_supervisor import run_subtitle_ocr_job

_VIDEO = os.environ.get("CONTENT_BOT_OCR_GPU_SMOKE_VIDEO")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_output(command: list[str], *, cwd: Path | None = None) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _run_provenance(video: Path) -> dict:
    repo = Path(__file__).resolve().parents[2]
    source_paths = (
        "backend/app/services/subtitle_ocr.py",
        "backend/app/services/subtitle_ocr_supervisor.py",
        "backend/app/services/subtitle_ocr_worker.py",
        "backend/tests/test_subtitle_ocr_gpu_smoke.py",
        "backend/pyproject.toml",
        "backend/runtimes/ocr_gpu/requirements.txt",
        "backend/runtimes/ocr_gpu/site-packages/runtime-manifest.json",
    )
    source_hashes = {
        name: _sha256_file(repo / name)
        for name in source_paths
        if (repo / name).is_file()
    }
    head = _command_output(["git", "rev-parse", "HEAD"], cwd=repo)
    dirty = _command_output(["git", "status", "--porcelain"], cwd=repo)
    models_dir = (
        Path(sys.executable).resolve().parent.parent
        / "Lib"
        / "site-packages"
        / "rapidocr_onnxruntime"
        / "models"
    )
    model_hashes = {
        model.name: _sha256_file(model)
        for model in sorted(models_dir.glob("*.onnx"))
    }
    gpu_snapshot = _command_output(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader",
        ]
    )
    active_power_scheme = _command_output(["powercfg", "/getactivescheme"])
    runtime_manifest = (
        Path(settings.subtitle_ocr_gpu_site_packages).resolve()
        / "runtime-manifest.json"
    )
    return {
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "host": {
            "platform": platform.platform(),
            "windows_version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": sys.version,
            "gpu_snapshot_csv": gpu_snapshot,
            "active_power_scheme": active_power_scheme,
        },
        "source": {
            "git_head": head,
            "git_worktree_dirty": bool(dirty),
            "relevant_file_sha256": source_hashes,
        },
        "input": {
            "path": str(video),
            "size_bytes": video.stat().st_size,
            "sha256": _sha256_file(video),
        },
        "models_sha256": model_hashes,
        "runtime_manifest_sha256": (
            _sha256_file(runtime_manifest) if runtime_manifest.is_file() else None
        ),
    }


def test_run_provenance_captures_input_and_relevant_sources(tmp_path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"test media fingerprint")

    provenance = _run_provenance(video)

    assert provenance["input"]["sha256"] == hashlib.sha256(
        b"test media fingerprint"
    ).hexdigest()
    assert (
        "backend/app/services/subtitle_ocr_supervisor.py"
        in provenance["source"]["relevant_file_sha256"]
    )
    assert "python" in provenance["host"]


@pytest.mark.skipif(not _VIDEO, reason="requires CONTENT_BOT_OCR_GPU_SMOKE_VIDEO")
def test_supervised_cuda_ocr_on_real_video(tmp_path):
    video = Path(_VIDEO).resolve()
    assert video.is_file(), f"GPU smoke video does not exist: {video}"
    media = probe_media(video)
    reference_path = os.environ.get("CONTENT_BOT_OCR_GPU_SMOKE_REFERENCE")
    output_path = os.environ.get("CONTENT_BOT_OCR_GPU_SMOKE_OUTPUT")
    provenance = _run_provenance(video) if output_path else None
    run_started_at_utc = datetime.now(UTC).isoformat()

    started = perf_counter()
    result = run_subtitle_ocr_job(
        video,
        SubtitleOcrRegion(x=10, y=86, width=78, height=14),
        media,
        device_policy="cuda",
        gpu_site_packages=settings.subtitle_ocr_gpu_site_packages,
        worker_timeout_seconds=1800,
        context=None,
        cache_dir=tmp_path / "cache",
        source_language="zh",
        sample_fps=3.0,
        min_duration_ms=300,
        max_gap_ms=250,
        auto_probe=False,
    )

    runtime = result["runtime"]
    assert runtime["effective_device"] == "cuda"
    assert runtime["onnxruntime_version"] == "1.26.0"
    assert set(runtime["stage_providers"]) == {"detector", "classifier", "recognizer"}
    assert all(
        providers[0] == "CUDAExecutionProvider"
        for providers in runtime["stage_providers"].values()
    )
    assert result["metrics"]["decoded_frames"] > 0

    reference_sha256 = None
    if reference_path:
        reference = json.loads(Path(reference_path).read_text(encoding="utf-8"))
        reference_sha256 = reference["sha256"]
        assert _sha256_file(video) == reference_sha256
        actual_cues = [
            {key: cue[key] for key in ("start_ms", "end_ms", "text")}
            for cue in result["document"]["segments"]
        ]
        reference_cues = [
            {key: cue[key] for key in ("start_ms", "end_ms", "text")}
            for cue in reference["segments"]
        ]
        assert actual_cues == reference_cues

    evidence = {
        "schema_version": 1,
        "run_kind": "supervised_gpu_ocr_smoke",
        "run_started_at_utc": run_started_at_utc,
        "run_finished_at_utc": datetime.now(UTC).isoformat(),
        "invocation": {
            "python_executable": sys.executable,
            "pytest_module_args": sys.argv,
            "working_directory": os.getcwd(),
            "video_env": _VIDEO,
            "reference_env": reference_path,
        },
        "elapsed_seconds": round(perf_counter() - started, 3),
        "config": {
            "region_percent": {"x": 10, "y": 86, "width": 78, "height": 14},
            "source_language": "zh",
            "sample_fps": 3.0,
            "min_duration_ms": 300,
            "max_gap_ms": 250,
            "auto_probe": False,
            "result_cache_policy": "fresh_tmp_dir_per_test_run_no_reuse",
        },
        "runtime": runtime,
        "runtime_metrics": result.get("runtime_metrics"),
        "metrics": result["metrics"],
        "timings_seconds": result.get("timings_seconds"),
        "segment_count": result["segment_count"],
        "segments": result["document"]["segments"],
        "reference": {
            "compared": bool(reference_path),
            "sha256": reference_sha256,
        },
    }
    if output_path:
        evidence["provenance"] = provenance
        atomic_json(Path(output_path).resolve(), evidence)
    print(
        json.dumps(
            {
                "output_path": str(Path(output_path).resolve()) if output_path else None,
                "elapsed_seconds": evidence["elapsed_seconds"],
                "worker_wall_seconds": (result.get("runtime_metrics") or {}).get(
                    "worker_wall_seconds"
                ),
                "segment_count": evidence["segment_count"],
                "device": runtime["effective_device"],
                "stage_providers": runtime["stage_providers"],
                "reference_compared": evidence["reference"]["compared"],
                "metrics": evidence["metrics"],
            },
            ensure_ascii=True,
        )
    )
