"""ASR API boundary: native inference can be terminated without stopping the backend."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .gemini_media import atomic_json
from .owned_process import OwnedProcess
from .process_metrics import nvidia_memory_snapshot, process_rss_bytes
from .subtitle_asr import SubtitleAsrError
from .subtitle_jobs import SubtitleJobCanceled


def run_subtitle_asr_job(video_path, media, *, context=None, **options):
    payload = {
        "video_path": str(Path(video_path).resolve()),
        "media": media,
        **{
            k: str(v.resolve()) if isinstance(v, Path) else v
            for k, v in options.items()
        },
    }
    if context:
        context.raise_if_canceled()
    with tempfile.TemporaryDirectory(prefix="content-bot-asr-") as directory:
        root = Path(directory)
        atomic_json(root / "request.json", payload)
        (root / "heartbeat").touch()
        with (root / "worker.log").open("w", encoding="utf-8") as log:
            child = OwnedProcess(
                [sys.executable, "-m", "app.services.subtitle_asr_worker", str(root)],
                cwd=Path(__file__).resolve().parents[2],
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
            last_gpu_sample = 0.0
            stage_deadline = time.monotonic() + 900
            try:
                # Handshake prevents model/ffmpeg startup before process-tree ownership is established.
                (root / "ready").touch()
                while child.process.poll() is None:
                    now = time.monotonic()
                    rss = process_rss_bytes(child.process.pid)
                    if rss is not None:
                        peak_rss_bytes = max(peak_rss_bytes, rss)
                    if now - last_gpu_sample >= 0.5:
                        for gpu in nvidia_memory_snapshot():
                            peak_gpu_memory[gpu["index"]] = max(
                                peak_gpu_memory.get(gpu["index"], 0),
                                gpu["used_bytes"],
                            )
                        last_gpu_sample = now
                    if context:
                        context.raise_if_canceled()
                    (root / "heartbeat").touch()
                    progress_path = root / "progress.json"
                    if progress_path.exists():
                        try:
                            progress = json.loads(
                                progress_path.read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            # Windows can briefly deny a read while the worker
                            # replaces the progress file. The next poll will
                            # observe the complete JSON; the job must continue.
                            progress = None
                        if progress is not None and progress != previous:
                            previous = progress
                            stage_deadline = time.monotonic() + 900
                            if context:
                                context.update(
                                    progress["progress"],
                                    progress["phase"],
                                    progress["message"],
                                )
                    if now > stage_deadline:
                        raise SubtitleAsrError(
                            "ASR quá 15 phút ở một bước xử lý; worker đã được thu hồi."
                        )
                    time.sleep(0.1)
                if context:
                    context.raise_if_canceled()
                if (root / "error.json").exists():
                    error = json.loads(
                        (root / "error.json").read_text(encoding="utf-8")
                    )
                    if error.get("canceled"):
                        raise SubtitleJobCanceled(error["message"])
                    raise SubtitleAsrError(error["message"])
                if child.process.returncode or not (root / "result.json").exists():
                    raise SubtitleAsrError(
                        f"ASR worker kết thúc bất thường (mã {child.process.returncode})."
                    )
                result = json.loads((root / "result.json").read_text(encoding="utf-8"))
                if peak_rss_bytes or peak_gpu_memory:
                    runtime_metrics = {
                        "worker_peak_rss_bytes": peak_rss_bytes,
                        "worker_peak_rss_source": "GetProcessMemoryInfo"
                        if os.name == "nt"
                        else "/proc/<pid>/status",
                    }
                    if peak_gpu_memory:
                        runtime_metrics.update(
                            {
                                "gpu_peak_memory_bytes": peak_gpu_memory,
                                "gpu_peak_memory_source": "nvidia-smi",
                            }
                        )
                    result["runtime_metrics"] = runtime_metrics
                return result
            finally:
                child.close()
