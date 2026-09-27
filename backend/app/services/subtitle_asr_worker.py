"""Short-lived ASR worker: one model per job, bounded PCM, supervised process tree."""

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path


def main():
    root = Path(sys.argv[1])
    heartbeat = root / "heartbeat"

    def watch():
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

    threading.Thread(target=watch, daemon=True).start()
    while not (root / "ready").exists():
        time.sleep(0.05)
    from .gemini_media import atomic_json
    from .subtitle_asr import extract_subtitles_asr
    from .subtitle_jobs import SubtitleJobCanceled

    class Context:
        def raise_if_canceled(self):
            if (root / "cancel").exists():
                raise SubtitleJobCanceled("Đã hủy ASR.")

        def update(self, progress, phase, message):
            self.raise_if_canceled()
            atomic_json(
                root / "progress.json",
                {"progress": progress, "phase": phase, "message": message},
            )

    try:
        payload = json.loads((root / "request.json").read_text(encoding="utf-8"))
        for key in ["video_path", "model_dir", "cache_dir"]:
            if payload.get(key) is not None:
                payload[key] = Path(payload[key])
        atomic_json(
            root / "result.json", extract_subtitles_asr(**payload, context=Context())
        )
    except SubtitleJobCanceled:
        atomic_json(root / "error.json", {"canceled": True, "message": "Đã hủy ASR."})
    except Exception as exc:
        atomic_json(root / "error.json", {"message": str(exc)[-2000:]})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
