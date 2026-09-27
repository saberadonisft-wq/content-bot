from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_subtitle_ocr_ground_truth.py"
SPEC = importlib.util.spec_from_file_location("subtitle_ocr_ground_truth", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
ground_truth = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ground_truth
SPEC.loader.exec_module(ground_truth)


OCCURRENCE_FIELDS = [
    "occurrence_id",
    "text_raw",
    "start_ms",
    "end_ms",
    "start_min_ms",
    "start_max_ms",
    "end_min_ms",
    "end_max_ms",
    "start_censored",
    "end_censored",
    "roi_or_line",
    "text_observability",
    "review_status",
    "reviewer",
    "reviewed_at_utc",
    "notes",
]
WINDOW_FIELDS = [
    "window_id",
    "start_ms",
    "end_ms",
    "review_status",
    "caption_status",
    "occurrence_ids",
    "reviewer",
    "reviewed_at_utc",
    "notes",
]
CANDIDATE_FIELDS = [
    "candidate_id",
    "run_id",
    "source",
    "candidate_text",
    "candidate_start_ms",
    "candidate_end_ms",
    "confidence",
    "occurrence_ids",
    "structure_status",
    "content_status",
    "timing_status",
    "reviewer",
    "reviewed_at_utc",
    "notes",
]


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _reviewed_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _occurrence(
    occurrence_id: str,
    text: str,
    start: int,
    end: int,
    *,
    observability: str = "legible",
) -> dict[str, str]:
    return {
        "occurrence_id": occurrence_id,
        "text_raw": text,
        "start_ms": str(start),
        "end_ms": str(end),
        "start_min_ms": str(start - 100),
        "start_max_ms": str(start + 100),
        "end_min_ms": str(end - 100),
        "end_max_ms": str(end + 100),
        "start_censored": "no",
        "end_censored": "no",
        "roi_or_line": "bottom, line 1",
        "text_observability": observability,
        "review_status": "confirmed",
        "reviewer": "reviewer-a",
        "reviewed_at_utc": _reviewed_at(),
        "notes": "",
    }


def _window(
    window_id: str,
    start: int,
    end: int,
    occurrence_ids: list[str],
    *,
    review_status: str = "complete",
) -> dict[str, str]:
    return {
        "window_id": window_id,
        "start_ms": str(start),
        "end_ms": str(end),
        "review_status": review_status,
        "caption_status": "captions_found" if occurrence_ids else "none_visible",
        "occurrence_ids": json.dumps(occurrence_ids),
        "reviewer": "reviewer-a",
        "reviewed_at_utc": _reviewed_at(),
        "notes": "",
    }


def _candidate(
    candidate_id: str,
    run_id: str,
    source: str,
    occurrence_ids: list[str],
    *,
    candidate_start_ms: int = 100,
    candidate_end_ms: int = 400,
    candidate_text: str = "字幕",
    structure_status: str = "matched",
    content_status: str = "exact",
    timing_status: str = "within_bounds",
) -> dict[str, str]:
    return {
        "candidate_id": candidate_id,
        "run_id": run_id,
        "source": source,
        "candidate_text": candidate_text,
        "candidate_start_ms": str(candidate_start_ms),
        "candidate_end_ms": str(candidate_end_ms),
        "confidence": "0.9",
        "occurrence_ids": json.dumps(occurrence_ids),
        "structure_status": structure_status,
        "content_status": content_status,
        "timing_status": timing_status,
        "reviewer": "reviewer-a",
        "reviewed_at_utc": _reviewed_at(),
        "notes": "",
    }


def _package(tmp_path: Path, occurrences: list[dict[str, str]]) -> Path:
    package = tmp_path / "package"
    package.mkdir()
    duration = 10_000
    source_video = package / "source-video.fixture"
    source_video.write_bytes(b"ground-truth source video fixture")
    source_video_sha = hashlib.sha256(source_video.read_bytes()).hexdigest().upper()
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "video_path_at_generation": str(source_video.resolve()),
                "video_sha256": source_video_sha,
                "video_stream": {"duration_ms_from_first_frame": duration},
            }
        ),
        encoding="utf-8",
    )
    _write_csv(package / "occurrences.csv", OCCURRENCE_FIELDS, occurrences)
    occurrence_ids = [row["occurrence_id"] for row in occurrences]
    _write_csv(
        package / "review_windows.csv",
        WINDOW_FIELDS,
        [
            _window("window-001", 0, duration, occurrence_ids),
        ],
    )
    reconciliation = package / "reconciliation"
    reconciliation.mkdir()
    return package


def _write_reconciliation(
    package: Path,
    candidates: list[dict[str, str]],
    expected: list[dict[str, str]],
) -> dict[str, str]:
    reconciliation = package / "reconciliation"
    source_by_old_run = {row["run_id"]: row["source"] for row in expected}
    source_video_sha = json.loads((package / "manifest.json").read_text(encoding="utf-8"))["video_sha256"]
    for source in ("fps3", "fps1"):
        if source not in source_by_old_run.values():
            source_by_old_run[f"empty-{source}"] = source
    run_id_map: dict[str, str] = {}
    metrics_by_source: dict[str, tuple[dict, Path, str]] = {}
    for old_run_id, source in source_by_old_run.items():
        run_candidates = [row for row in candidates if row["run_id"] == old_run_id]
        sample_fps = 3 if source == "fps3" else 1
        metrics = {
            "sha256": source_video_sha,
            "sample_fps": sample_fps,
            "segments": [
                {
                    "start_ms": int(row["candidate_start_ms"]),
                    "end_ms": int(row["candidate_end_ms"]),
                    "text": row["candidate_text"],
                    "confidence": float(row["confidence"]) if row["confidence"] else None,
                }
                for row in run_candidates
            ],
        }
        filename = "baseline-metrics.json" if source == "fps3" else "comparison-metrics.json"
        path = reconciliation / filename
        path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        run_id_map[old_run_id] = digest[:16]
        metrics_by_source[source] = (metrics, path, digest)

    for row in expected:
        old_run_id = row["run_id"]
        source = row["source"]
        index = int(row["candidate_id"].rsplit("-", 1)[1])
        row["run_id"] = run_id_map[old_run_id]
        row["candidate_id"] = f"{row['run_id']}-{source}-{index:03d}"
    for row in candidates:
        old_run_id = row["run_id"]
        source = row["source"]
        index = int(row["candidate_id"].rsplit("-", 1)[1])
        row["run_id"] = run_id_map[old_run_id]
        row["candidate_id"] = f"{row['run_id']}-{source}-{index:03d}"
    snapshot = [
        {
            "candidate_id": row["candidate_id"],
            "run_id": row["run_id"],
            "source": row["source"],
            "candidate_text": row["candidate_text"],
            "candidate_start_ms": int(row["candidate_start_ms"]),
            "candidate_end_ms": int(row["candidate_end_ms"]),
            "confidence": float(row["confidence"]) if row["confidence"] else None,
        }
        for row in candidates
    ]
    snapshot_path = reconciliation / "candidate-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    baseline = metrics_by_source["fps3"]
    comparison = metrics_by_source["fps1"]
    (reconciliation / "reconciliation-manifest.json").write_text(
        json.dumps(
            {
                "source_video_sha256": source_video_sha,
                "video_stream": {"duration_ms_from_first_frame": 10_000},
                "baseline_run_id": run_id_map[next(run for run, source in source_by_old_run.items() if source == "fps3")],
                "baseline_segment_count": len(baseline[0]["segments"]),
                "baseline_sample_fps": baseline[0]["sample_fps"],
                "baseline_metrics_snapshot": "baseline-metrics.json",
                "baseline_metrics_sha256": baseline[2],
                "comparison_run_id": run_id_map[next(run for run, source in source_by_old_run.items() if source == "fps1")],
                "comparison_segment_count": len(comparison[0]["segments"]),
                "comparison_sample_fps": comparison[0]["sample_fps"],
                "comparison_metrics_snapshot": "comparison-metrics.json",
                "comparison_metrics_sha256": comparison[2],
                "candidate_snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest().upper(),
                "expected_candidates": expected,
            }
        ),
        encoding="utf-8",
    )
    _write_csv(reconciliation / "candidate_review.csv", CANDIDATE_FIELDS, candidates)
    return run_id_map


def test_full_blind_inventory_and_candidate_mapping_report_split_merge_miss_and_extra(
    tmp_path: Path,
) -> None:
    occurrences = [
        _occurrence("occ-001", "第一句", 100, 400),
        _occurrence("occ-002", "第二句", 800, 1_000),
    ]
    package = _package(tmp_path, occurrences)
    # The one full-video window lists both occurrences, so the inventory is complete.
    expected = [
        {"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"},
        {"candidate_id": "run-a-fps3-002", "run_id": "run-a", "source": "fps3"},
        {"candidate_id": "run-a-fps3-003", "run_id": "run-a", "source": "fps3"},
        {"candidate_id": "run-b-fps1-001", "run_id": "run-b", "source": "fps1"},
    ]
    candidates = [
        _candidate(
            "run-a-fps3-001",
            "run-a",
            "fps3",
            ["occ-001", "occ-002"],
            candidate_end_ms=1_000,
            structure_status="split_merge",
            timing_status="not_applicable",
        ),
        _candidate(
            "run-a-fps3-002",
            "run-a",
            "fps3",
            ["occ-001"],
            structure_status="split",
            timing_status="not_applicable",
        ),
        _candidate(
            "run-a-fps3-003",
            "run-a",
            "fps3",
            [],
            structure_status="extra",
            content_status="false_positive",
            timing_status="not_applicable",
        ),
        _candidate(
            "run-b-fps1-001",
            "run-b",
            "fps1",
            [],
            structure_status="extra",
            content_status="false_positive",
            timing_status="not_applicable",
        ),
    ]
    run_id_map = _write_reconciliation(package, candidates, expected)

    lock, freeze_errors = ground_truth.freeze_blind(package)
    report, validation_errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert lock["status"] == "frozen"
    assert validation_errors == []
    assert report["run_summary"][run_id_map["run-a"]]["split_candidates"] == 1
    assert report["run_summary"][run_id_map["run-a"]]["merged_candidates"] == 1
    assert report["run_summary"][run_id_map["run-a"]]["false_positive_candidates"] == 1
    assert report["run_summary"][run_id_map["run-a"]]["missed_occurrences"] == 0
    assert report["run_summary"][run_id_map["run-b"]]["missed_occurrences"] == 2
    assert report["run_summary"][run_id_map["run-b"]]["false_positive_candidates"] == 1


def test_validator_rejects_gap_and_unreviewed_window(tmp_path: Path) -> None:
    package = _package(tmp_path, [])
    _write_csv(
        package / "review_windows.csv",
        WINDOW_FIELDS,
        [
            _window("window-001", 0, 4_999, []),
            _window("window-002", 5_000, 10_000, [], review_status="pending"),
        ],
    )

    _, errors = ground_truth.validate_blind_inventory(package)

    assert any("gap" in error for error in errors)
    assert any("review_status must be complete" in error for error in errors)


def test_validator_requires_occurrence_in_every_intersected_window(tmp_path: Path) -> None:
    package = _package(tmp_path, [_occurrence("occ-001", "字幕", 4_900, 5_100)])
    _write_csv(
        package / "review_windows.csv",
        WINDOW_FIELDS,
        [
            _window("window-001", 0, 5_000, ["occ-001"]),
            _window("window-002", 5_000, 10_000, []),
        ],
    )

    _, errors = ground_truth.validate_blind_inventory(package)

    assert any("window-002" in error and "missing=['occ-001']" in error for error in errors)


def test_validator_allows_unreadable_caption_for_presence_and_timing_only(tmp_path: Path) -> None:
    unreadable = _occurrence("occ-001", "", 100, 400, observability="unreadable")
    package = _package(tmp_path, [unreadable])
    expected = [{"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"}]
    candidates = [
        _candidate(
            "run-a-fps3-001",
            "run-a",
            "fps3",
            ["occ-001"],
            content_status="not_scored",
        )
    ]
    run_id_map = _write_reconciliation(package, candidates, expected)

    _, freeze_errors = ground_truth.freeze_blind(package)
    report, validation_errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert validation_errors == []
    assert report["run_summary"][run_id_map["run-a"]]["linked_occurrences"] == 1


def test_any_unreadable_occurrence_in_merge_requires_not_scored_content(tmp_path: Path) -> None:
    occurrences = [
        _occurrence("occ-001", "可读", 100, 400),
        _occurrence("occ-002", "", 500, 900, observability="partially_unreadable"),
    ]
    package = _package(tmp_path, occurrences)
    expected = [{"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"}]
    candidate = _candidate(
        "run-a-fps3-001",
        "run-a",
        "fps3",
        ["occ-001", "occ-002"],
        candidate_start_ms=100,
        candidate_end_ms=900,
        structure_status="merge",
        content_status="exact",
    )
    _write_reconciliation(package, [candidate], expected)
    _, freeze_errors = ground_truth.freeze_blind(package)

    _, validation_errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert any("any unreadable linked text requires content_status=not_scored" in error for error in validation_errors)


def test_validator_computes_timing_instead_of_trusting_review_status(tmp_path: Path) -> None:
    package = _package(tmp_path, [_occurrence("occ-001", "字幕", 100, 400)])
    expected = [{"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"}]
    candidate = _candidate(
        "run-a-fps3-001",
        "run-a",
        "fps3",
        ["occ-001"],
        candidate_start_ms=300,
        candidate_end_ms=400,
        timing_status="within_bounds",
    )
    run_id_map = _write_reconciliation(package, [candidate], expected)
    _, freeze_errors = ground_truth.freeze_blind(package)

    _, validation_errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert any(
        run_id_map["run-a"] in error and "computed status is 'timing_error'" in error
        for error in validation_errors
    )


def test_split_merge_mapping_must_overlap_each_linked_occurrence(tmp_path: Path) -> None:
    package = _package(
        tmp_path,
        [
            _occurrence("occ-001", "第一句", 100, 400),
            _occurrence("occ-002", "第二句", 800, 1_000),
        ],
    )
    expected = [
        {"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"},
        {"candidate_id": "run-a-fps3-002", "run_id": "run-a", "source": "fps3"},
    ]
    candidates = [
        _candidate(
            "run-a-fps3-001",
            "run-a",
            "fps3",
            ["occ-001", "occ-002"],
            structure_status="split_merge",
            timing_status="not_applicable",
        ),
        _candidate(
            "run-a-fps3-002",
            "run-a",
            "fps3",
            ["occ-001"],
            structure_status="split",
            timing_status="not_applicable",
        ),
    ]
    _write_reconciliation(package, candidates, expected)
    _, freeze_errors = ground_truth.freeze_blind(package)

    _, validation_errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert any("does not overlap linked occurrence occ-002" in error for error in validation_errors)


def test_validator_checks_one_to_one_exact_text_against_ground_truth(tmp_path: Path) -> None:
    package = _package(tmp_path, [_occurrence("occ-001", "字幕", 100, 400)])
    expected = [{"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"}]
    candidate = _candidate(
        "run-a-fps3-001",
        "run-a",
        "fps3",
        ["occ-001"],
        candidate_text="錯字",
    )
    _write_reconciliation(package, [candidate], expected)
    _, freeze_errors = ground_truth.freeze_blind(package)

    _, incorrect_exact_errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert any(
        "normalized one-to-one text comparison is 'text_error'" in error
        for error in incorrect_exact_errors
    )
    candidate["content_status"] = "text_error"
    _write_csv(package / "reconciliation" / "candidate_review.csv", CANDIDATE_FIELDS, [candidate])

    report, corrected_errors = ground_truth.validate_package(package)

    assert corrected_errors == []
    assert report["run_summary"]


def test_freeze_rehashes_source_video_and_rejects_changed_file(tmp_path: Path) -> None:
    package = _package(tmp_path, [])
    (package / "source-video.fixture").write_bytes(b"different video pixels")

    _, errors = ground_truth.freeze_blind(package)

    assert any("Source video file SHA-256 differs" in error for error in errors)


def test_uncertain_window_is_recorded_but_blocks_freeze(tmp_path: Path) -> None:
    package = _package(tmp_path, [])
    _write_csv(
        package / "review_windows.csv",
        WINDOW_FIELDS,
        [
            {
                **_window("window-001", 0, 10_000, []),
                "caption_status": "uncertain",
                "notes": "possible caption but not resolvable",
            }
        ],
    )

    _, errors = ground_truth.freeze_blind(package)

    assert any("caption presence must be adjudicated" in error for error in errors)


def test_validation_rejects_candidate_table_edited_after_freeze(tmp_path: Path) -> None:
    package = _package(tmp_path, [_occurrence("occ-001", "字幕", 100, 400)])
    expected = [{"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"}]
    _write_reconciliation(
        package,
        [_candidate("run-a-fps3-001", "run-a", "fps3", ["occ-001"])],
        expected,
    )
    _, freeze_errors = ground_truth.freeze_blind(package)
    with (package / "occurrences.csv").open("a", encoding="utf-8") as stream:
        stream.write("\n")

    _, errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert any("changed after blind inventory was frozen" in error for error in errors)


def test_validation_rejects_candidate_text_changed_from_generated_snapshot(tmp_path: Path) -> None:
    package = _package(tmp_path, [_occurrence("occ-001", "字幕", 100, 400)])
    expected = [{"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"}]
    candidates = [_candidate("run-a-fps3-001", "run-a", "fps3", ["occ-001"])]
    _write_reconciliation(package, candidates, expected)
    _, freeze_errors = ground_truth.freeze_blind(package)
    changed_candidate = dict(candidates[0])
    changed_candidate["candidate_text"] = "reviewer changed the OCR output"
    _write_csv(
        package / "reconciliation" / "candidate_review.csv",
        CANDIDATE_FIELDS,
        [changed_candidate],
    )

    _, errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert any("candidate_text differs from the generated OCR snapshot" in error for error in errors)


def test_validation_rejects_snapshot_detached_from_metrics_even_if_review_matches(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, [_occurrence("occ-001", "字幕", 100, 400)])
    expected = [{"candidate_id": "run-a-fps3-001", "run_id": "run-a", "source": "fps3"}]
    candidates = [_candidate("run-a-fps3-001", "run-a", "fps3", ["occ-001"])]
    _write_reconciliation(package, candidates, expected)
    _, freeze_errors = ground_truth.freeze_blind(package)
    reconciliation = package / "reconciliation"
    snapshot_path = reconciliation / "candidate-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot[0]["candidate_text"] = "detached from OCR metrics"
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    manifest_path = reconciliation / "reconciliation-manifest.json"
    recon_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recon_manifest["candidate_snapshot_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest().upper()
    manifest_path.write_text(json.dumps(recon_manifest, indent=2) + "\n", encoding="utf-8")
    candidates[0]["candidate_text"] = snapshot[0]["candidate_text"]
    _write_csv(reconciliation / "candidate_review.csv", CANDIDATE_FIELDS, candidates)

    _, errors = ground_truth.validate_package(package)

    assert freeze_errors == []
    assert any("differs from its OCR metrics snapshot" in error for error in errors)
