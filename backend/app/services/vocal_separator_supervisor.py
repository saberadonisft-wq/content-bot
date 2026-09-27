"""Own native separation inference and publish validated stems only on success."""
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .gemini_media import atomic_json
from .owned_process import OwnedProcess
from .subtitle_jobs import SubtitleJobCanceled
from .vocal_audio import VocalSeparatorError, check, wave_stats


def run_vocal_separation_job(audio_path, output_dir, *, context=None,
                             timeout_seconds=7200, **options):
    check(context)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".separation-job-", dir=output_dir) as tmp:
        root = Path(tmp)
        staged = root / "artifacts"
        staged.mkdir()
        payload = {"audio_path": str(Path(audio_path).resolve()), "output_dir": str(staged),
                   **{key: str(value.resolve()) if isinstance(value, Path) else value
                      for key, value in options.items()}}
        atomic_json(root / "request.json", payload)
        (root / "heartbeat").touch()
        with (root / "worker.log").open("w", encoding="utf-8") as log:
            child = OwnedProcess(
                [sys.executable, "-m", "app.services.vocal_separator_worker", str(root)],
                cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log,
                env={**os.environ, "PYTHONUTF8": "1", "OMP_NUM_THREADS": "4",
                     "MKL_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"},
            )
            deadline = time.monotonic() + timeout_seconds
            previous = None
            try:
                (root / "ready").touch()
                while child.process.poll() is None:
                    check(context)
                    (root / "heartbeat").touch()
                    if time.monotonic() >= deadline:
                        # A cancellation can become observable at the same
                        # instant as the timeout.  Give it precedence so the
                        # caller never reports a canceled job as a timeout.
                        check(context)
                        raise VocalSeparatorError("Job tách nền quá thời gian; worker đã được thu hồi.")
                    progress_path = root / "progress.json"
                    if progress_path.exists():
                        progress = json.loads(progress_path.read_text(encoding="utf-8"))
                        if progress != previous:
                            previous = progress
                            if context:
                                context.update(progress["progress"], progress["phase"], progress["message"])
                    time.sleep(.1)
                check(context)
                if (root / "error.json").exists():
                    error = json.loads((root / "error.json").read_text(encoding="utf-8"))
                    if error.get("canceled"):
                        raise SubtitleJobCanceled(error["message"])
                    raise VocalSeparatorError(error["message"])
                if child.process.returncode or not (root / "result.json").exists():
                    raise VocalSeparatorError(f"Worker tách nền kết thúc bất thường ({child.process.returncode}).")
                result = json.loads((root / "result.json").read_text(encoding="utf-8"))
            finally:
                child.close()
        # Worker can no longer mutate outputs after this point.
        stems = result.get("stems", {})
        expected = {"vocals", "background"} if result.get("method") == "demucs" else {"background"}
        if not stems or set(stems) != expected:
            raise VocalSeparatorError("Worker không trả đủ stem.")
        relative = {}
        for name, value in stems.items():
            path = Path(value).resolve()
            if not path.is_relative_to(staged.resolve()) or not path.is_file():
                raise VocalSeparatorError("Đường dẫn stem không thuộc job hiện tại.")
            wave_stats(path, context)
            relative[name] = path.relative_to(staged.resolve())
        check(context)
        final = output_dir / f"separation-{uuid.uuid4().hex}"
        result["stems"] = {name: str(final / rel) for name, rel in relative.items()}
        atomic_json(staged / "result.json", result)
        check(context)
        staged.replace(final)
        return result
