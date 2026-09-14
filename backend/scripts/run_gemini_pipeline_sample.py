"""Run an authorized local media sample, keeping a reviewable JSON and SRT artifact."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.credential_resolver import credential, gemini_credentials
from app.services.gemini_media import atomic_json
from app.services.media_probe import probe_media


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--model", default=None)
    parser.add_argument("--cooldown", action="append", default=[], help="Previously observed model=UTC-ISO-time; preserve quota waits across benchmark processes")
    args = parser.parse_args()
    if args.baseline:
        source = subprocess.check_output(["git", "show", "4cc8758:backend/app/services/gemini_subtitles.py"], text=True, encoding="utf-8")
        module = types.ModuleType("app.services._gemini_baseline")
        sys.modules[module.__name__] = module
        exec(compile(source, "gemini_baseline.py", "exec"), module.__dict__)  # noqa: S102 -- fixed local baseline revision
    else:
        from app.services import gemini_subtitles as module
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    cpu_started = time.process_time()
    metrics = {"pid": os.getpid(), "http": {}, "phases": {}, "scope": "Python process; CPU excludes FFmpeg children"}
    metrics_lock = threading.Lock()
    def save_metrics():
        metrics["wall_seconds"] = round(time.monotonic() - started, 3)
        metrics["python_cpu_seconds"] = round(time.process_time() - cpu_started, 3)
        atomic_json(args.output / "metrics.json", metrics)
    class Context:
        job_id = "sample-baseline" if args.baseline else "sample-pipeline"
        cancel_event = threading.Event()
        def raise_if_canceled(self):
            if self.cancel_event.is_set():
                raise RuntimeError("Canceled")
        def update(self, progress, phase, message):
            print(f"{progress}% {phase}: {message}", flush=True)
            with metrics_lock:
                metrics["phases"].setdefault(phase, round(time.monotonic() - started, 3))
                metrics["last_status"] = {"progress": progress, "phase": phase, "message": message}
                save_metrics()
        def update_details(self, details):
            atomic_json(args.output / "progress.json", details)
    signal.signal(signal.SIGINT, lambda *_: Context.cancel_event.set())
    def watch_stop():
        while not Context.cancel_event.wait(1):
            if (args.output / "STOP").exists():
                Context.cancel_event.set()
    threading.Thread(target=watch_stop, daemon=True).start()
    service = module.GeminiSubtitleService(module.GeminiSubtitleSettings(job_root=args.output / "jobs"),
        api_key_provider=lambda: credential("gemini_api_key"),
        **({"keyring_provider": gemini_credentials} if not args.baseline else {}))
    for item in args.cooldown:
        from app.services.gemini_dispatch import ApiFailure
        limited_model, until = item.split("=", 1)
        reset = datetime.fromisoformat(until)
        if reset.tzinfo is None:
            raise ValueError("Cooldown needs an explicit UTC offset")
        remaining = max(0, (reset - datetime.now(UTC)).total_seconds())
        for row in service.runtime_keys():
            service.dispatcher.report(row, limited_model, ApiFailure(429, "generateContent", "quota", remaining, "daily"))
    original_client = service._client
    def instrumented_client():
        client = original_client()
        def record(response):
            # Retain only operation/status, never request URLs, headers or secrets.
            path = response.request.url.path
            operation = "generate" if ":generateContent" in path else "file"
            key = f"{operation}:{response.request.method}:{response.status_code}"
            with metrics_lock:
                metrics["http"][key] = metrics["http"].get(key, 0) + 1
                if operation == "generate":
                    response.read()
                    responses = args.output / "responses"
                    responses.mkdir(exist_ok=True)
                    text = response.text
                    secret = response.request.headers.get("x-goog-api-key")
                    if secret:
                        text = text.replace(secret, "[redacted]")
                    if len(text.encode("utf-8")) <= 8_000_000:
                        (responses / f"{sum(count for name, count in metrics['http'].items() if name.startswith('generate:')):04d}.json").write_text(text, encoding="utf-8")
                if response.status_code == 429:
                    from app.services.gemini_dispatch import classify_failure
                    response.read()
                    failure = classify_failure(response, "generateContent" if operation == "generate" else "file")
                    metrics["last_quota"] = {"kind": failure.quota_kind, "retry_after_seconds": failure.wait_seconds,
                                             "model": path.rsplit("/", 1)[-1].split(":", 1)[0] if operation == "generate" else "*",
                                             "observed_at": datetime.now(UTC).isoformat(),
                                             "retry_not_before": (datetime.now(UTC) + timedelta(seconds=failure.wait_seconds)).isoformat()}
                save_metrics()
        client.event_hooks.setdefault("response", []).append(record)
        return client
    service._client = instrumented_client
    video = args.video
    if args.seconds > 0:
        video = args.output / "sample.mp4"
        identity = {"source": str(args.video.resolve()), "size": args.video.stat().st_size,
                    "mtime": args.video.stat().st_mtime_ns, "seconds": args.seconds,
                    "proxy_version": "baseline-4cc8758" if args.baseline else "source-time-v1"}
        identity_path = args.output / "sample-identity.json"
        try:
            reusable = video.is_file() and json.loads(identity_path.read_text()) == identity
        except (OSError, ValueError):
            reusable = False
        if not reusable:
            service._create_proxy(args.video, video, start_seconds=0, duration_seconds=args.seconds, cancel_event=Context.cancel_event)
            identity_path.write_text(json.dumps(identity), encoding="utf-8")
    result = service.generate(video, probe_media(video), {"bilingual": True, **({"model": args.model} if args.model else {})}, Context())
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "subtitles.srt").write_text(result["srt"], encoding="utf-8-sig")
    print(f"Saved {result['segment_count']} cues to {args.output}", flush=True)
    save_metrics()


if __name__ == "__main__":
    main()
