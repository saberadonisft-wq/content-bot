"""Short-lived separation runtime with supervisor heartbeat and startup handshake."""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path


def main():
    root = Path(sys.argv[1])

    def watch():
        while True:
            try:
                expired = time.time() - (root / "heartbeat").stat().st_mtime > 30
            except OSError:
                expired = True
            if expired:
                if os.name != "nt":
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(75)
            time.sleep(1)

    threading.Thread(target=watch, daemon=True).start()
    while not (root / "ready").exists():
        time.sleep(.05)
    from .gemini_media import atomic_json
    from .subtitle_jobs import SubtitleJobCanceled
    from .vocal_separator import separate_vocals

    class Context:
        def raise_if_canceled(self):
            if (root / "cancel").exists():
                raise SubtitleJobCanceled("Đã hủy tách nền.")

        def update(self, progress, phase, message):
            self.raise_if_canceled()
            atomic_json(root / "progress.json",
                        {"progress": progress, "phase": phase, "message": message})

    try:
        payload = json.loads((root / "request.json").read_text(encoding="utf-8"))
        atomic_json(root / "result.json", separate_vocals(**payload, context=Context()))
    except SubtitleJobCanceled:
        atomic_json(root / "error.json", {"canceled": True, "message": "Đã hủy tách nền."})
    except Exception as exc:
        atomic_json(root / "error.json", {"message": str(exc)[-2000:]})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
