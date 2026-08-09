"""Benchmark energy alignment against the controlled Vietnamese golden fixtures."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import wave
from pathlib import Path

from app.services.subtitle_alignment import (
    AlignmentSettings,
    AudioWindow,
    _align_energy_window,
)


def parse_args() -> argparse.Namespace:
    repository = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=repository / "backend" / "tests" / "fixtures" / "alignment",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=repository / "docs" / "SUBTITLE_ALIGNMENT_BENCHMARK.md",
    )
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _pcm_bytes(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise RuntimeError(f"Fixture must be mono PCM16: {path}")
        sample_rate = stream.getframerate()
        frame_count = stream.getnframes()
        payload = stream.readframes(frame_count)
    if sample_rate != 16_000:
        raise RuntimeError(f"Fixture must use 16 kHz audio: {path}")
    return payload, round(frame_count * 1000 / sample_rate)


def evaluate_dataset(fixture_dir: Path) -> dict[str, object]:
    labels = json.loads((fixture_dir / "labels.json").read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for item in labels["cases"]:
        pcm, duration_ms = _pcm_bytes(fixture_dir / item["file"])
        cue = {
            "id": item["id"],
            "start_ms": item["coarse_start_ms"],
            "end_ms": item["coarse_end_ms"],
            "text": item["transcript"],
            "timing_source": "gemini_estimate",
            "timing_precision_ms": 1000,
            "confidence": 0.6,
            "needs_review": False,
            "revision": 0,
        }
        aligned, warnings = _align_energy_window(
            [cue],
            [cue],
            AudioWindow(0, duration_ms, (str(item["id"]),)),
            pcm,
            duration_ms,
            AlignmentSettings(engine="energy"),
        )
        result = aligned[str(item["id"])]
        predicted_start = int(result.get("speech_start_ms", cue["start_ms"]))
        predicted_end = int(result.get("speech_end_ms", cue["end_ms"]))
        rows.append(
            {
                "id": item["id"],
                "category": item["category"],
                "expected_start_ms": item["speech_start_ms"],
                "predicted_start_ms": predicted_start,
                "start_error_ms": abs(predicted_start - int(item["speech_start_ms"])),
                "expected_end_ms": item["speech_end_ms"],
                "predicted_end_ms": predicted_end,
                "end_error_ms": abs(predicted_end - int(item["speech_end_ms"])),
                "needs_review": bool(result.get("needs_review")),
                "warning_count": len(warnings),
            }
        )
    start_errors = [int(row["start_error_ms"]) for row in rows]
    end_errors = [int(row["end_error_ms"]) for row in rows]
    combined = start_errors + end_errors
    return {
        "case_count": len(rows),
        "start_mae_ms": round(statistics.mean(start_errors), 2),
        "start_median_ms": statistics.median(start_errors),
        "start_p95_ms": _percentile(start_errors, 0.95),
        "end_mae_ms": round(statistics.mean(end_errors), 2),
        "end_median_ms": statistics.median(end_errors),
        "end_p95_ms": _percentile(end_errors, 0.95),
        "combined_median_ms": statistics.median(combined),
        "combined_p95_ms": _percentile(combined, 0.95),
        "rows": rows,
    }


def write_report(result: dict[str, object], report: Path) -> None:
    rows = result["rows"]
    assert isinstance(rows, list)
    lines = [
        "# Benchmark căn chỉnh phụ đề tiếng Việt",
        "",
        (
            "> Bộ golden synthetic dùng Microsoft An TTS với biên lời được dựng có kiểm soát. "
            "Bộ này phát hiện regression của VAD/timing; không thay thế đánh giá audio người thật cho ASR."
        ),
        "",
        "## Kết quả",
        "",
        "| Chỉ số | Start | End |",
        "| --- | ---: | ---: |",
        f"| MAE | {result['start_mae_ms']} ms | {result['end_mae_ms']} ms |",
        f"| Median | {result['start_median_ms']} ms | {result['end_median_ms']} ms |",
        f"| P95 | {result['start_p95_ms']} ms | {result['end_p95_ms']} ms |",
        "",
        (
            "Ngưỡng chấp nhận: combined median ≤ 80 ms và combined P95 ≤ 200 ms. "
            f"Kết quả: median {result['combined_median_ms']} ms; "
            f"P95 {result['combined_p95_ms']} ms."
        ),
        "",
        "## Chi tiết fixture",
        "",
        "| Trường hợp | Nhóm | Sai số start | Sai số end | Cần duyệt |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in rows:
        assert isinstance(row, dict)
        lines.append(
            f"| {row['id']} | {row['category']} | {row['start_error_ms']} ms | "
            f"{row['end_error_ms']} ms | {'Có' if row['needs_review'] else 'Không'} |"
        )
    lines.extend(
        [
            "",
            "Chạy lại:",
            "",
            "```powershell",
            ".\\backend\\.venv\\Scripts\\python.exe backend\\scripts\\benchmark_subtitle_alignment.py --check",
            "```",
            "",
        ]
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    result = evaluate_dataset(args.fixture_dir.resolve())
    write_report(result, args.report.resolve())
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}))
    if args.check and (
        float(result["combined_median_ms"]) > 80
        or int(result["combined_p95_ms"]) > 200
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
