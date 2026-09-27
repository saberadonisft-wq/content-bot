"""Measure the actual supervised ASR pipeline on the labeled local speech corpus."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import sys
import time
import unicodedata
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.process_metrics import nvidia_memory_snapshot
from app.services.subtitle_asr_supervisor import run_subtitle_asr_job


def distance(left, right):
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b))
            )
        previous = current
    return previous[-1]


def normalize(text):
    # Keep diacritics and numeric spelling differences. Do not rewrite the reference to fit ASR.
    return " ".join(
        "".join(
            c if c.isalnum() or c.isspace() else " "
            for c in unicodedata.normalize("NFC", text).lower()
        ).split()
    )


def main():
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--compute-type", default="int8")
    args = parser.parse_args()
    args.corpus = args.corpus or repo_root / "backend/tests/fixtures/alignment/labels.json"
    args.output = args.output or repo_root / "backend/artifacts/subtitle-remediation/phase3/corpus-cpu.json"
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    report = {
        "provenance": corpus["provenance"],
        "model": args.model,
        "device": args.device,
        "compute_type": args.compute_type,
        "language": "auto",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "faster_whisper": importlib.metadata.version("faster-whisper"),
        "notes": [
            "Generated speech, not natural multilingual video.",
            "WER uses whitespace tokens (Vietnamese syllables); punctuation and case ignored, digits and diacritics retained.",
            "Timing compares first/last observed words with labeled speech envelope; no per-word ground truth.",
            "One uncached worker process per case; no live API or model downloads.",
            "worker_peak_rss_bytes is sampled from the supervised worker process; GPU memory is reported only when nvidia-smi is available.",
        ],
        "gpu_memory_before": nvidia_memory_snapshot(),
        "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    for case in corpus["cases"]:
        path = (args.corpus.parent / case["file"]).resolve()
        with wave.open(str(path), "rb") as audio:
            duration = round(audio.getnframes() * 1000 / audio.getframerate())
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        gpu_before = nvidia_memory_snapshot()
        start = time.monotonic()
        try:
            result = run_subtitle_asr_job(
                path,
                {
                    "fingerprint": fingerprint,
                    "audio_hash": fingerprint,
                    "has_audio": True,
                    "duration_ms": duration,
                    "source_start_ms": 0,
                },
                source_language="auto",
                model_name=args.model,
                device=args.device,
                compute_type=args.compute_type,
                allow_download=False,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            gpu_after = nvidia_memory_snapshot()
            row = {
                **case,
                "sha256": fingerprint,
                "duration_ms": duration,
                "elapsed_seconds": round(elapsed, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "gpu_memory_before": gpu_before,
                "gpu_memory_after": gpu_after,
            }
            report["cases"].append(row)
            failures.append(row)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps({"case": case["id"], "error": str(exc)}), flush=True)
            continue
        elapsed = time.monotonic() - start
        gpu_after = nvidia_memory_snapshot()
        cues = result["document"]["segments"]
        hypothesis = " ".join(cue["text"] for cue in cues)
        expected, actual = normalize(case["transcript"]), normalize(hypothesis)
        word_errors = distance(expected.split(), actual.split())
        char_errors = distance(expected.replace(" ", ""), actual.replace(" ", ""))
        words = [word for cue in cues for word in (cue.get("words") or [])]
        row = {
            **case,
            "sha256": fingerprint,
            "duration_ms": duration,
            "elapsed_seconds": round(elapsed, 3),
            "hypothesis": hypothesis,
            "word_errors": word_errors,
            "reference_words": len(expected.split()),
            "char_errors": char_errors,
            "reference_chars": len(expected.replace(" ", "")),
            "wer": word_errors / len(expected.split()),
            "cer": char_errors / len(expected.replace(" ", "")),
            "start_error_ms": min(word["start_ms"] for word in words)
            - case["speech_start_ms"]
            if words
            else None,
            "end_error_ms": max(word["end_ms"] for word in words)
            - case["speech_end_ms"]
            if words
            else None,
            "result": result,
            "worker_peak_rss_bytes": (result.get("runtime_metrics") or {}).get(
                "worker_peak_rss_bytes"
            ),
            "gpu_peak_memory_bytes": (result.get("runtime_metrics") or {}).get(
                "gpu_peak_memory_bytes", {}
            ),
            "gpu_memory_before": gpu_before,
            "gpu_memory_after": gpu_after,
        }
        report["cases"].append(row)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "case": case["id"],
                    "seconds": row["elapsed_seconds"],
                    "wer": row["wer"],
                    "start_error_ms": row["start_error_ms"],
                    "end_error_ms": row["end_error_ms"],
                }
            ),
            flush=True,
        )
    rows = report["cases"]
    successful = [row for row in rows if "result" in row]
    errors = sorted(
        abs(row[key])
        for row in successful
        for key in ["start_error_ms", "end_error_ms"]
        if row[key] is not None
    )
    report["summary"] = {
        "case_count": len(rows),
        "successful_cases": len(successful),
        "failed_cases": len(failures),
        "wer": sum(row["word_errors"] for row in successful)
        / sum(row["reference_words"] for row in successful)
        if successful
        else None,
        "cer": sum(row["char_errors"] for row in successful)
        / sum(row["reference_chars"] for row in successful)
        if successful
        else None,
        "timing_median_absolute_ms": statistics.median(errors) if errors else None,
        "timing_p95_absolute_ms": errors[
            max(0, __import__("math").ceil(0.95 * len(errors)) - 1)
        ]
        if errors
        else None,
        "total_seconds": sum(row["elapsed_seconds"] for row in rows),
        "missing_speech_cases": sum(
            not row["result"]["document"]["segments"] for row in successful
        ),
        "worker_peak_rss_max_bytes": max(
            (row["worker_peak_rss_bytes"] or 0 for row in successful), default=0
        ),
        "gpu_peak_memory_max_bytes": {
            str(index): max(
                (
                    row.get("gpu_peak_memory_bytes", {}).get(
                        index,
                        row.get("gpu_peak_memory_bytes", {}).get(str(index), 0),
                    )
                    if isinstance(row.get("gpu_peak_memory_bytes", {}), dict)
                    else 0
                )
                for row in successful
            )
            for index in sorted(
                {
                    int(index)
                    for row in successful
                    for index in (row.get("gpu_peak_memory_bytes", {}) or {})
                }
            )
        },
        "gpu_observed": bool(report["gpu_memory_before"])
        or any(row["gpu_memory_before"] or row["gpu_memory_after"] for row in rows),
        "gpu_inference_used": report["device"] == "cuda" and bool(successful),
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"]), flush=True)
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
