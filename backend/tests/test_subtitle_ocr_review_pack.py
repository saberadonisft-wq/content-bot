from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_subtitle_ocr_review_pack.py"
SPEC = importlib.util.spec_from_file_location("subtitle_ocr_review_pack", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
review_pack = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review_pack
SPEC.loader.exec_module(review_pack)


def _candidate(candidate_id: str, text: str, start_ms: int, end_ms: int) -> dict:
    return {
        "candidate_id": candidate_id,
        "source": "test",
        "text": text,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "confidence": 0.9,
    }


def test_exact_text_with_changed_boundaries_is_reported_as_timing_review() -> None:
    baseline = [_candidate("fps3-001", "same", 100, 500)]
    comparison = [_candidate("fps1-001", "same", 200, 500)]

    result = review_pack.build_candidate_diff(baseline, comparison)

    assert result["differences"] == []
    assert result["timing_differences"] == [
        {
            "change_id": "timing-001",
            "baseline_candidate_id": "fps3-001",
            "comparison_candidate_id": "fps1-001",
            "text": "same",
            "baseline_start_ms": 100,
            "comparison_start_ms": 200,
            "start_delta_ms": 100,
            "baseline_end_ms": 500,
            "comparison_end_ms": 500,
            "end_delta_ms": 0,
            "start_ms": 100,
            "end_ms": 500,
            "resolution": "pending_human_review",
        }
    ]
    assert "not ground truth" in result["interpretation"]


def test_sequence_change_remains_an_advisory_candidate_difference() -> None:
    baseline = [_candidate("fps3-001", "old", 100, 500)]
    comparison = [_candidate("fps1-001", "new", 100, 500)]

    result = review_pack.build_candidate_diff(baseline, comparison)

    assert result["differences"][0]["operation"] == "replace"
    assert result["differences"][0]["baseline_candidate_ids"] == ["fps3-001"]
    assert result["differences"][0]["comparison_candidate_ids"] == ["fps1-001"]
    assert result["timing_differences"] == []


def test_occurrence_csv_starts_empty_without_candidate_hints(tmp_path: Path) -> None:
    output = tmp_path / "occurrences.csv"
    review_pack.write_occurrences_csv(output)

    import csv

    with output.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)

    assert rows == []
    assert "text_raw" in (reader.fieldnames or [])
    assert "candidate_id" not in (reader.fieldnames or [])
    assert "candidate_text" not in (reader.fieldnames or [])


def test_review_windows_partition_video_without_candidate_priority(tmp_path: Path) -> None:
    output = tmp_path / "review_windows.csv"
    review_pack.write_review_windows_csv(output, 12_300)

    import csv

    with output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert [(row["start_ms"], row["end_ms"]) for row in rows] == [
        ("0", "5000"),
        ("5000", "10000"),
        ("10000", "12300"),
    ]
    assert all("priority" not in " ".join(row) for row in rows)


def test_video_duration_includes_last_frame_display_interval() -> None:
    from fractions import Fraction

    duration_ms, method = review_pack.resolve_video_duration_ms(
        first_absolute_ms=2_000,
        last_frame_start_ms=1_010,
        stream_start_time=2_000,
        stream_duration=1_020,
        stream_time_base=Fraction(1, 1000),
        last_frame_pts=3_010,
        last_frame_duration=10,
        last_frame_time_base=Fraction(1, 1000),
        fallback_frame_interval_ms=33.4,
    )

    assert duration_ms == 1_020
    assert method == "stream_start_plus_duration+last_frame_pts_plus_duration"
