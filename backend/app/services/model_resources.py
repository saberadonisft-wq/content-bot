"""OS-owned local model slot shared with the existing voice worker."""

import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def gpu_model_slot(device, check_canceled=lambda: None, *, timeout_seconds=1800):
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
        except ImportError:
            device = "cpu"
    if device != "cuda":
        yield
        return
    path = Path(
        os.environ.get(
            "VOICE_WORKER_LOCK",
            Path(__file__).resolve().parents[3]
            / "runtimes"
            / "voiceover"
            / ".worker.lock",
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        while True:
            check_canceled()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Hết thời gian chờ GPU. Tác vụ giọng/ASR khác đang sử dụng model slot."
                    )
                time.sleep(0.1)
        try:
            check_canceled()
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
