from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OCCURRENCE_FIELDS = {
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
}
WINDOW_FIELDS = {
    "window_id",
    "start_ms",
    "end_ms",
    "review_status",
    "caption_status",
    "occurrence_ids",
    "reviewer",
    "reviewed_at_utc",
    "notes",
}
CANDIDATE_FIELDS = {
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
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def normalize_exact_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return value


def read_csv_rows(path: Path, required_fields: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    if not path.is_file():
        return [], [f"Missing file: {path.name}"]
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        missing = required_fields - fields
        errors = [f"{path.name} is missing columns: {', '.join(sorted(missing))}"] if missing else []
        rows = []
        for raw_row in reader:
            if not any((value or "").strip() for value in raw_row.values()):
                continue
            if None in raw_row:
                errors.append(f"{path.name} contains a row with extra CSV fields")
            rows.append({key: value or "" for key, value in raw_row.items() if key is not None})
    return rows, errors


def parse_int(value: str, label: str, errors: list[str]) -> int | None:
    try:
        if not value.strip():
            raise ValueError
        return int(value.strip())
    except ValueError:
        errors.append(f"{label} must be a whole number in milliseconds; got {value!r}")
        return None


def validate_utc(value: str, label: str, errors: list[str]) -> None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        errors.append(f"{label} must be an ISO-8601 UTC timestamp")
        return
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        errors.append(f"{label} must include a UTC timezone (Z or +00:00)")


def parse_id_list(value: str, label: str, errors: list[str]) -> list[str] | None:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        errors.append(f"{label} must be a JSON array of IDs")
        return None
    if not isinstance(parsed, list) or any(not isinstance(item, str) or not item.strip() for item in parsed):
        errors.append(f"{label} must be a JSON array of non-empty strings")
        return None
    if len(parsed) != len(set(parsed)):
        errors.append(f"{label} contains a duplicate ID")
    return parsed


def _manifest(package: Path, errors: list[str]) -> dict[str, Any]:
    path = package / "manifest.json"
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"Cannot read manifest.json: {exc}")
        return {}
    stream = value.get("video_stream")
    if (
        not isinstance(stream, dict)
        or not isinstance(stream.get("duration_ms_from_first_frame"), int)
        or isinstance(stream.get("duration_ms_from_first_frame"), bool)
    ):
        errors.append("manifest.json must record integer video_stream.duration_ms_from_first_frame")
    video_sha = value.get("video_sha256")
    if (
        not isinstance(video_sha, str)
        or len(video_sha) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in video_sha)
    ):
        errors.append("manifest.json must record the source video SHA-256")
    video_path_value = value.get("video_path_at_generation")
    if not isinstance(video_path_value, str) or not video_path_value.strip():
        errors.append("manifest.json must record video_path_at_generation for source verification")
    else:
        video_path = Path(video_path_value)
        if not video_path.is_absolute():
            video_path = REPO_ROOT / video_path
        if not video_path.is_file():
            errors.append(f"Cannot verify the generated source video file: {video_path}")
        elif isinstance(video_sha, str) and sha256_file(video_path) != video_sha.upper():
            errors.append("Source video file SHA-256 differs from manifest.json")
    return value


def validate_blind_inventory(package: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    manifest = _manifest(package, errors)
    stream = manifest.get("video_stream", {})
    duration = stream.get("duration_ms_from_first_frame") if isinstance(stream, dict) else None
    if not isinstance(duration, int) or duration <= 0:
        errors.append("Video duration must be a positive whole number of milliseconds")
        duration = 0

    occurrence_rows, occurrence_errors = read_csv_rows(package / "occurrences.csv", OCCURRENCE_FIELDS)
    window_rows, window_errors = read_csv_rows(package / "review_windows.csv", WINDOW_FIELDS)
    errors.extend(occurrence_errors)
    errors.extend(window_errors)

    occurrences: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(occurrence_rows, start=2):
        prefix = f"occurrences.csv row {index}"
        occurrence_id = row.get("occurrence_id", "").strip()
        if not occurrence_id:
            errors.append(f"{prefix}: occurrence_id is required")
            continue
        if occurrence_id in occurrences:
            errors.append(f"{prefix}: duplicate occurrence_id {occurrence_id!r}")
            continue
        row_values: dict[str, Any] = {"occurrence_id": occurrence_id}
        for field in (
            "start_ms",
            "end_ms",
            "start_min_ms",
            "start_max_ms",
            "end_min_ms",
            "end_max_ms",
        ):
            row_values[field] = parse_int(row.get(field, ""), f"{prefix} {field}", errors)
        if (
            row_values["start_ms"] is not None
            and row_values["end_ms"] is not None
            and not (0 <= row_values["start_ms"] < row_values["end_ms"] <= duration)
        ):
            errors.append(f"{prefix}: expected 0 <= start_ms < end_ms <= video duration")
        for low_field, point_field, high_field, label in (
            ("start_min_ms", "start_ms", "start_max_ms", "start"),
            ("end_min_ms", "end_ms", "end_max_ms", "end"),
        ):
            low, point, high = (row_values[field] for field in (low_field, point_field, high_field))
            if all(value is not None for value in (low, point, high)) and not (
                0 <= low <= point <= high <= duration
            ):
                errors.append(f"{prefix}: {label} point and uncertainty bounds must be ordered within the video")
        for field in ("start_censored", "end_censored"):
            if row.get(field, "").strip().lower() not in {"yes", "no"}:
                errors.append(f"{prefix}: {field} must be yes or no")
        if row.get("start_censored", "").strip().lower() == "yes" and any(
            row_values.get(field) != 0 for field in ("start_ms", "start_min_ms", "start_max_ms")
        ):
            errors.append(f"{prefix}: a left-censored occurrence must have start time and bounds at 0")
        if row.get("end_censored", "").strip().lower() == "yes" and any(
            row_values.get(field) != duration for field in ("end_ms", "end_min_ms", "end_max_ms")
        ):
            errors.append(f"{prefix}: a right-censored occurrence must have end time and bounds at video duration")
        observability = row.get("text_observability", "").strip()
        if observability not in {"legible", "partially_unreadable", "unreadable"}:
            errors.append(f"{prefix}: text_observability must be legible, partially_unreadable, or unreadable")
        if observability == "legible" and not row.get("text_raw", "").strip():
            errors.append(f"{prefix}: legible occurrences require text_raw")
        if row.get("review_status", "").strip() != "confirmed":
            errors.append(f"{prefix}: occurrence must be confirmed; ambiguous rows block OCR scoring")
        if not row.get("reviewer", "").strip():
            errors.append(f"{prefix}: reviewer is required")
        validate_utc(row.get("reviewed_at_utc", ""), f"{prefix} reviewed_at_utc", errors)
        occurrences[occurrence_id] = {**row, **row_values}

    windows: list[dict[str, Any]] = []
    seen_window_ids: set[str] = set()
    for index, row in enumerate(window_rows, start=2):
        prefix = f"review_windows.csv row {index}"
        window_id = row.get("window_id", "").strip()
        if not window_id:
            errors.append(f"{prefix}: window_id is required")
        elif window_id in seen_window_ids:
            errors.append(f"{prefix}: duplicate window_id {window_id!r}")
        seen_window_ids.add(window_id)
        start = parse_int(row.get("start_ms", ""), f"{prefix} start_ms", errors)
        end = parse_int(row.get("end_ms", ""), f"{prefix} end_ms", errors)
        ids = parse_id_list(row.get("occurrence_ids", ""), f"{prefix} occurrence_ids", errors)
        if row.get("review_status", "").strip() != "complete":
            errors.append(f"{prefix}: review_status must be complete")
        caption_status = row.get("caption_status", "").strip()
        if caption_status not in {"captions_found", "none_visible", "uncertain"}:
            errors.append(f"{prefix}: caption_status must be captions_found, none_visible, or uncertain")
        if caption_status == "uncertain":
            errors.append(f"{prefix}: caption presence must be adjudicated before blind inventory freeze")
        if ids is not None and caption_status != "uncertain" and (
            (ids and caption_status != "captions_found") or (not ids and caption_status != "none_visible")
        ):
            errors.append(f"{prefix}: caption_status must agree with occurrence_ids")
        if not row.get("reviewer", "").strip():
            errors.append(f"{prefix}: reviewer is required")
        validate_utc(row.get("reviewed_at_utc", ""), f"{prefix} reviewed_at_utc", errors)
        if start is not None and end is not None and not (0 <= start < end <= duration):
            errors.append(f"{prefix}: expected 0 <= start_ms < end_ms <= video duration")
        windows.append({**row, "window_id": window_id, "start_ms_value": start, "end_ms_value": end, "ids": ids})

    windows.sort(key=lambda item: (item["start_ms_value"] is None, item["start_ms_value"] or 0))
    cursor = 0
    for row in windows:
        start, end = row["start_ms_value"], row["end_ms_value"]
        if start is None or end is None:
            continue
        if start != cursor:
            relation = "gap" if start > cursor else "overlap"
            errors.append(f"review_windows.csv has a {relation}: expected next start_ms={cursor}, got {start}")
        cursor = end
    if not windows:
        errors.append("review_windows.csv must cover the whole video; it has no windows")
    elif cursor != duration:
        errors.append(f"review_windows.csv ends at {cursor} ms; expected video duration {duration} ms")

    occurrence_ids = set(occurrences)
    for row in windows:
        ids = row["ids"]
        if ids is None:
            continue
        unknown = set(ids) - occurrence_ids
        if unknown:
            errors.append(f"{row['window_id']}: unknown occurrence IDs: {', '.join(sorted(unknown))}")
        start, end = row["start_ms_value"], row["end_ms_value"]
        if start is None or end is None:
            continue
        expected = {
            occurrence_id
            for occurrence_id, occurrence in occurrences.items()
            if occurrence.get("start_min_ms") is not None
            and occurrence.get("end_max_ms") is not None
            and occurrence["start_min_ms"] < end
            and occurrence["end_max_ms"] > start
        }
        if set(ids) != expected:
            errors.append(
                f"{row['window_id']}: occurrence_ids must list every occurrence whose possible interval intersects this window; "
                f"missing={sorted(expected - set(ids))}, extra={sorted(set(ids) - expected)}"
            )

    return {
        "manifest": manifest,
        "duration_ms": duration,
        "occurrences": occurrences,
        "windows": windows,
    }, errors


def freeze_blind(package: Path) -> tuple[dict[str, Any], list[str]]:
    data, errors = validate_blind_inventory(package)
    if errors:
        return {}, errors
    manifest = data["manifest"]
    lock_path = package / "blind-inventory.lock.json"
    lock = {
        "status": "frozen",
        "frozen_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_video_sha256": manifest.get("video_sha256"),
        "manifest_sha256": sha256_file(package / "manifest.json"),
        "duration_ms": data["duration_ms"],
        "occurrences_sha256": sha256_file(package / "occurrences.csv"),
        "review_windows_sha256": sha256_file(package / "review_windows.csv"),
        "reviewers": sorted(
            {row.get("reviewer", "").strip() for row in data["windows"] if row.get("reviewer", "").strip()}
        ),
        "occurrence_count": len(data["occurrences"]),
        "window_count": len(data["windows"]),
    }
    if lock_path.exists():
        try:
            existing = read_json(lock_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return {}, [f"Existing blind inventory lock is unreadable: {exc}; archive it before re-freezing"]
        for field in (
            "source_video_sha256",
            "manifest_sha256",
            "duration_ms",
            "occurrences_sha256",
            "review_windows_sha256",
            "occurrence_count",
            "window_count",
        ):
            if existing.get(field) != lock[field]:
                return {}, [
                    (
                        "Blind inventory differs from its existing lock. Archive blind-inventory.lock.json only after "
                        "deliberately reopening the blind review, then complete and freeze it again."
                    )
                ]
        return existing, []
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lock, []


def validate_candidates(package: Path, data: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    lock_path = package / "blind-inventory.lock.json"
    try:
        lock = read_json(lock_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        errors.append(f"Cannot read blind-inventory.lock.json: {exc}")
        return {}
    if lock.get("status") != "frozen":
        errors.append("Blind inventory lock is not frozen")
    if lock.get("occurrences_sha256") != sha256_file(package / "occurrences.csv"):
        errors.append("occurrences.csv changed after blind inventory was frozen")
    if lock.get("review_windows_sha256") != sha256_file(package / "review_windows.csv"):
        errors.append("review_windows.csv changed after blind inventory was frozen")
    manifest = data["manifest"]
    if lock.get("manifest_sha256") != sha256_file(package / "manifest.json"):
        errors.append("manifest.json changed after blind inventory was frozen")
    if lock.get("duration_ms") != data["duration_ms"]:
        errors.append("Blind lock duration does not match manifest.json")
    if lock.get("occurrence_count") != len(data["occurrences"]):
        errors.append("Blind lock occurrence count does not match occurrences.csv")
    if lock.get("window_count") != len(data["windows"]):
        errors.append("Blind lock window count does not match review_windows.csv")
    if lock.get("source_video_sha256") != manifest.get("video_sha256"):
        errors.append("Blind lock source video does not match manifest.json")

    reconciliation = package / "reconciliation"
    try:
        recon_manifest = read_json(reconciliation / "reconciliation-manifest.json")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        errors.append(f"Cannot read reconciliation/reconciliation-manifest.json: {exc}")
        return {}
    blind_manifest = data["manifest"]
    if recon_manifest.get("source_video_sha256") != blind_manifest.get("video_sha256"):
        errors.append("Reconciliation source video does not match the frozen blind inventory")
    recon_stream = recon_manifest.get("video_stream")
    if (
        not isinstance(recon_stream, dict)
        or recon_stream.get("duration_ms_from_first_frame") != data["duration_ms"]
    ):
        errors.append("Reconciliation duration does not match the blind video timeline")

    run_specs = (
        ("baseline", "fps3", 3),
        ("comparison", "fps1", 1),
    )
    declared_runs: dict[str, str] = {}
    declared_counts: dict[str, int] = {}
    metrics_by_run: dict[str, dict[str, Any]] = {}
    for prefix, source, expected_fps in run_specs:
        run_id = recon_manifest.get(f"{prefix}_run_id")
        segment_count = recon_manifest.get(f"{prefix}_segment_count")
        sample_fps = recon_manifest.get(f"{prefix}_sample_fps")
        metrics_filename = recon_manifest.get(f"{prefix}_metrics_snapshot")
        metrics_digest = recon_manifest.get(f"{prefix}_metrics_sha256")
        if not isinstance(run_id, str) or not run_id:
            errors.append(f"Reconciliation manifest is missing {prefix}_run_id")
            continue
        if run_id in declared_runs:
            errors.append("Baseline and comparison OCR run IDs must be distinct")
        declared_runs[run_id] = source
        if not isinstance(segment_count, int) or isinstance(segment_count, bool) or segment_count < 0:
            errors.append(f"Reconciliation manifest has invalid {prefix}_segment_count")
            continue
        declared_counts[run_id] = segment_count
        sample_fps_matches = (
            isinstance(sample_fps, (int, float))
            and not isinstance(sample_fps, bool)
            and float(sample_fps) == expected_fps
        )
        if not sample_fps_matches:
            errors.append(f"{prefix} run must be the declared {expected_fps} FPS evaluation")
        if metrics_filename != f"{prefix}-metrics.json":
            errors.append(f"Reconciliation manifest has invalid {prefix} metrics snapshot filename")
            continue
        metrics_path = reconciliation / metrics_filename
        try:
            metrics = read_json(metrics_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append(f"Cannot read {prefix} metrics snapshot: {exc}")
            continue
        actual_metrics_digest = sha256_file(metrics_path)
        if metrics_digest != actual_metrics_digest:
            errors.append(f"{prefix} metrics snapshot hash does not match the reconciliation manifest")
        if run_id != actual_metrics_digest[:16]:
            errors.append(f"{prefix} run ID does not match its metrics snapshot hash")
        if str(metrics.get("sha256", "")).upper() != str(blind_manifest.get("video_sha256", "")).upper():
            errors.append(f"{prefix} metrics snapshot references a different source video")
        metrics_fps = metrics.get("sample_fps")
        metrics_fps_matches = (
            isinstance(metrics_fps, (int, float))
            and not isinstance(metrics_fps, bool)
            and float(metrics_fps) == expected_fps
        )
        if not metrics_fps_matches:
            errors.append(f"{prefix} metrics snapshot does not record {expected_fps} FPS")
        segments = metrics.get("segments")
        if not isinstance(segments, list) or len(segments) != segment_count:
            errors.append(f"{prefix} metrics snapshot segment count does not match the reconciliation manifest")
        else:
            metrics_by_run[run_id] = metrics
    snapshot_path = reconciliation / "candidate-snapshot.json"
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        errors.append(f"Cannot read reconciliation/candidate-snapshot.json: {exc}")
        return {}
    if not isinstance(snapshot, list):
        errors.append("Reconciliation candidate snapshot must be a JSON array")
        return {}
    if recon_manifest.get("candidate_snapshot_sha256") != sha256_file(snapshot_path):
        errors.append("candidate-snapshot.json hash does not match the reconciliation manifest")
    expected = recon_manifest.get("expected_candidates")
    if not isinstance(expected, list):
        errors.append("Reconciliation manifest must declare expected_candidates")
        return {}
    expected_by_id: dict[str, dict[str, str]] = {}
    for item in expected:
        if not isinstance(item, dict) or not all(isinstance(item.get(field), str) for field in ("candidate_id", "run_id", "source")):
            errors.append("Reconciliation manifest contains a malformed candidate identity")
            continue
        candidate_id = item["candidate_id"]
        if candidate_id in expected_by_id:
            errors.append(f"Reconciliation manifest has duplicate candidate_id {candidate_id!r}")
        expected_by_id[candidate_id] = item
        if item["run_id"] in declared_runs and item["source"] != declared_runs[item["run_id"]]:
            errors.append(f"Candidate {candidate_id} has the wrong source for its OCR run")
    expected_snapshot_ids = {
        f"{run_id}-{source}-{index:03d}"
        for run_id, source in declared_runs.items()
        for index in range(1, declared_counts.get(run_id, 0) + 1)
    }
    if set(expected_by_id) != expected_snapshot_ids:
        errors.append("Expected candidate identities do not cover the declared baseline/comparison metrics outputs")
    snapshot_by_id: dict[str, dict[str, Any]] = {}
    for item in snapshot:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str):
            errors.append("Candidate snapshot contains a malformed candidate")
            continue
        if item["candidate_id"] in snapshot_by_id:
            errors.append(f"Candidate snapshot has duplicate candidate_id {item['candidate_id']!r}")
        snapshot_by_id[item["candidate_id"]] = item
    if set(snapshot_by_id) != set(expected_by_id):
        errors.append("Candidate snapshot identities do not match the reconciliation manifest")
    for candidate_id, declared in expected_by_id.items():
        original = snapshot_by_id.get(candidate_id)
        if original is not None and any(
            original.get(field) != declared.get(field) for field in ("run_id", "source")
        ):
            errors.append(f"Candidate snapshot identity differs from manifest for {candidate_id}")
        if original is None:
            continue
        run_id = declared["run_id"]
        metrics = metrics_by_run.get(run_id)
        if metrics is None:
            continue
        try:
            index = int(candidate_id.rsplit("-", 1)[1]) - 1
            segment = metrics["segments"][index]
            source_fields = {
                "candidate_text": str(segment.get("text", "")),
                "candidate_start_ms": int(segment["start_ms"]),
                "candidate_end_ms": int(segment["end_ms"]),
                "confidence": segment.get("confidence"),
            }
        except (IndexError, KeyError, TypeError, ValueError, AttributeError):
            errors.append(f"Candidate {candidate_id} cannot be derived from its OCR metrics snapshot")
            continue
        if any(original.get(field) != expected_value for field, expected_value in source_fields.items()):
            errors.append(f"Candidate snapshot content differs from its OCR metrics snapshot for {candidate_id}")

    rows, csv_errors = read_csv_rows(reconciliation / "candidate_review.csv", CANDIDATE_FIELDS)
    errors.extend(csv_errors)
    seen: set[str] = set()
    by_run_occurrence: dict[str, Counter[str]] = defaultdict(Counter)
    candidate_rows: list[dict[str, Any]] = []
    known_occurrences = set(data["occurrences"])
    for index, row in enumerate(rows, start=2):
        prefix = f"candidate_review.csv row {index}"
        candidate_id = row.get("candidate_id", "").strip()
        if not candidate_id:
            errors.append(f"{prefix}: candidate_id is required")
            continue
        if candidate_id in seen:
            errors.append(f"{prefix}: duplicate candidate_id {candidate_id!r}")
        seen.add(candidate_id)
        declared = expected_by_id.get(candidate_id)
        if declared is None:
            errors.append(f"{prefix}: unexpected candidate_id {candidate_id!r}")
        elif (row.get("run_id", "").strip(), row.get("source", "").strip()) != (declared["run_id"], declared["source"]):
            errors.append(f"{prefix}: run_id/source does not match the declared OCR run")
        original = snapshot_by_id.get(candidate_id)
        if original is not None and row.get("candidate_text", "") != str(original.get("candidate_text", "")):
            errors.append(f"{prefix}: candidate_text differs from the generated OCR snapshot")
        ids = parse_id_list(row.get("occurrence_ids", ""), f"{prefix} occurrence_ids", errors)
        structure_status = row.get("structure_status", "").strip()
        content_status = row.get("content_status", "").strip()
        timing_status = row.get("timing_status", "").strip()
        if ids == []:
            if structure_status != "extra":
                errors.append(f"{prefix}: an empty occurrence_ids array must have structure_status=extra")
            if content_status != "false_positive" or timing_status != "not_applicable":
                errors.append(f"{prefix}: an empty occurrence_ids array must be false_positive/not_applicable")
        elif ids is not None:
            if structure_status not in {"matched", "split", "merge", "split_merge", "duplicate", "unresolved"}:
                errors.append(f"{prefix}: linked candidates require a valid structure_status")
            if content_status not in {"exact", "text_error", "not_scored", "unresolved"}:
                errors.append(f"{prefix}: linked candidates need content_status exact, text_error, not_scored, or unresolved")
            if timing_status not in {"within_bounds", "timing_error", "not_applicable", "unresolved"}:
                errors.append(f"{prefix}: linked candidates need timing_status within_bounds, timing_error, not_applicable, or unresolved")
            unknown = set(ids) - known_occurrences
            if unknown:
                errors.append(f"{prefix}: unknown occurrence IDs: {', '.join(sorted(unknown))}")
            linked_text_observability = {
                data["occurrences"][occurrence_id].get("text_observability")
                for occurrence_id in ids
                if occurrence_id in known_occurrences
            }
            if content_status == "not_scored" and linked_text_observability == {"legible"}:
                errors.append(f"{prefix}: not_scored requires at least one partially/unreadable linked occurrence")
            if linked_text_observability - {"legible"} and content_status != "not_scored":
                errors.append(f"{prefix}: any unreadable linked text requires content_status=not_scored")
            if (
                structure_status == "matched"
                and len(ids) == 1
                and ids[0] in known_occurrences
                and linked_text_observability == {"legible"}
                and content_status in {"exact", "text_error"}
            ):
                expected_text = normalize_exact_text(data["occurrences"][ids[0]].get("text_raw", ""))
                candidate_text = normalize_exact_text(row.get("candidate_text", ""))
                text_matches = candidate_text == expected_text
                if (content_status == "exact") != text_matches:
                    expected_status = "exact" if text_matches else "text_error"
                    errors.append(
                        f"{prefix}: content_status={content_status!r}; normalized one-to-one text comparison is {expected_status!r}"
                    )
            for occurrence_id in ids:
                by_run_occurrence[row.get("run_id", "").strip()][occurrence_id] += 1
        if content_status == "unresolved" or timing_status == "unresolved":
            errors.append(f"{prefix}: unresolved candidate adjudication blocks validation")
        if structure_status == "unresolved":
            errors.append(f"{prefix}: unresolved structure adjudication blocks validation")
        if not row.get("reviewer", "").strip():
            errors.append(f"{prefix}: reviewer is required")
        validate_utc(row.get("reviewed_at_utc", ""), f"{prefix} reviewed_at_utc", errors)
        candidate_start = parse_int(row.get("candidate_start_ms", ""), f"{prefix} candidate_start_ms", errors)
        candidate_end = parse_int(row.get("candidate_end_ms", ""), f"{prefix} candidate_end_ms", errors)
        if candidate_start is not None and candidate_end is not None and not (
            0 <= candidate_start < candidate_end <= data["duration_ms"]
        ):
            errors.append(f"{prefix}: candidate time range must be inside the source video")
        if original is not None and (
            candidate_start != original.get("candidate_start_ms")
            or candidate_end != original.get("candidate_end_ms")
        ):
            errors.append(f"{prefix}: candidate timing differs from the generated OCR snapshot")
        if candidate_start is not None and candidate_end is not None and ids is not None:
            for occurrence_id in ids:
                occurrence = data["occurrences"].get(occurrence_id)
                if occurrence is None:
                    continue
                possible_start = occurrence.get("start_min_ms")
                possible_end = occurrence.get("end_max_ms")
                if (
                    possible_start is not None
                    and possible_end is not None
                    and (candidate_start >= possible_end or candidate_end <= possible_start)
                ):
                    errors.append(
                        f"{prefix}: candidate interval does not overlap linked occurrence {occurrence_id}"
                    )
        original_confidence = None if original is None else original.get("confidence")
        supplied_confidence = row.get("confidence", "").strip()
        try:
            confidence_matches = (
                supplied_confidence == ""
                if original_confidence is None
                else float(supplied_confidence) == float(original_confidence)
            )
        except ValueError:
            confidence_matches = False
        if original is not None and not confidence_matches:
            errors.append(f"{prefix}: confidence differs from the generated OCR snapshot")
        candidate_rows.append({**row, "ids": ids, "structure_status_value": structure_status})

    missing = set(expected_by_id) - seen
    if missing:
        errors.append(f"candidate_review.csv is missing {len(missing)} OCR candidates: {', '.join(sorted(missing)[:8])}")

    run_ids = sorted(
        {item["run_id"] for item in expected_by_id.values()}
        | {
            run_id
            for run_id in (recon_manifest.get("baseline_run_id"), recon_manifest.get("comparison_run_id"))
            if isinstance(run_id, str) and run_id
        }
    )
    result: dict[str, Any] = {"run_summary": {}}
    for run_id in run_ids:
        run_candidates = [row for row in candidate_rows if row.get("run_id", "").strip() == run_id]
        per_occurrence_links = by_run_occurrence[run_id]
        linked_rows_by_occurrence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in run_candidates:
            for occurrence_id in row.get("ids") or []:
                if occurrence_id in known_occurrences:
                    linked_rows_by_occurrence[occurrence_id].append(row)
        for row in run_candidates:
            ids = row.get("ids") or []
            status = row.get("structure_status_value")
            if not ids:
                expected_statuses = {"extra"}
            elif len(ids) > 1:
                includes_multiple_links = any(per_occurrence_links[item] > 1 for item in ids if item in known_occurrences)
                expected_statuses = {"split_merge", "merge"} if includes_multiple_links else {"merge"}
            elif ids[0] in known_occurrences and per_occurrence_links[ids[0]] > 1:
                expected_statuses = {"split", "duplicate"}
            else:
                expected_statuses = {"matched"}
            if status not in expected_statuses:
                errors.append(
                    f"candidate_review.csv candidate {row.get('candidate_id')}: structure_status={status!r}; "
                    f"expected one of {sorted(expected_statuses)} from its mapping"
                )
        for row in run_candidates:
            ids = row.get("ids") or []
            structure = row["structure_status_value"]
            timing_status = row.get("timing_status", "").strip()
            if not ids or structure in {"split", "duplicate", "split_merge"}:
                expected_timing_status = "not_applicable"
            else:
                linked = [data["occurrences"][item] for item in ids if item in known_occurrences]
                if not linked:
                    expected_timing_status = "not_applicable"
                else:
                    first = min(linked, key=lambda item: item["start_ms"])
                    last = max(linked, key=lambda item: item["end_ms"])
                    comparable_edges: list[bool] = []
                    start_censored = first.get("start_censored", "").strip().lower() == "yes"
                    end_censored = last.get("end_censored", "").strip().lower() == "yes"
                    if not start_censored:
                        candidate_start = parse_int(
                            row.get("candidate_start_ms", ""),
                            f"candidate {row.get('candidate_id')} candidate_start_ms",
                            errors,
                        )
                        if candidate_start is not None:
                            comparable_edges.append(first["start_min_ms"] <= candidate_start <= first["start_max_ms"])
                    if not end_censored:
                        candidate_end = parse_int(
                            row.get("candidate_end_ms", ""),
                            f"candidate {row.get('candidate_id')} candidate_end_ms",
                            errors,
                        )
                        if candidate_end is not None:
                            comparable_edges.append(last["end_min_ms"] <= candidate_end <= last["end_max_ms"])
                    if not comparable_edges:
                        expected_timing_status = "not_applicable"
                    else:
                        expected_timing_status = (
                            "within_bounds" if all(comparable_edges) else "timing_error"
                        )
            if timing_status != expected_timing_status:
                errors.append(
                    f"candidate_review.csv candidate {row.get('candidate_id')}: timing_status={timing_status!r}; "
                    f"computed status is {expected_timing_status!r} from linked visual boundary bounds"
                )
        misses = sorted(known_occurrences - set(per_occurrence_links))
        extras = sum(row.get("ids") == [] for row in run_candidates)
        split_candidates = sum(row["structure_status_value"] == "split" for row in run_candidates)
        duplicate_candidates = sum(row["structure_status_value"] == "duplicate" for row in run_candidates)
        split_merge_candidates = sum(row["structure_status_value"] == "split_merge" for row in run_candidates)
        merged_candidates = sum(
            row["structure_status_value"] in {"merge", "split_merge"} for row in run_candidates
        )
        result["run_summary"][run_id] = {
            "candidate_count": len(run_candidates),
            "false_positive_candidates": extras,
            "missed_occurrences": len(misses),
            "missed_occurrence_ids": misses,
            "split_candidates": split_candidates,
            "duplicate_candidates": duplicate_candidates,
            "split_merge_candidates": split_merge_candidates,
            "merged_candidates": merged_candidates,
            "linked_occurrences": len(per_occurrence_links),
            "machine_checked_one_to_one_text_candidates": sum(
                row.get("structure_status_value") == "matched"
                and len(row.get("ids") or []) == 1
                and row.get("content_status", "").strip() in {"exact", "text_error"}
                for row in run_candidates
            ),
            "structural_timing_unscored_candidates": sum(
                row.get("structure_status_value") in {"split", "duplicate", "split_merge"}
                for row in run_candidates
            ),
        }
    return result


def validate_package(package: Path) -> tuple[dict[str, Any], list[str]]:
    data, errors = validate_blind_inventory(package)
    if errors:
        return {}, errors
    candidate_summary = validate_candidates(package, data, errors)
    if errors:
        return {}, errors
    report = {
        "status": "passed",
        "validated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_video_sha256": data["manifest"].get("video_sha256"),
        "duration_ms": data["duration_ms"],
        "occurrence_count": len(data["occurrences"]),
        "review_window_count": len(data["windows"]),
        "occurrences_sha256": sha256_file(package / "occurrences.csv"),
        "review_windows_sha256": sha256_file(package / "review_windows.csv"),
        "candidate_snapshot_sha256": sha256_file(
            package / "reconciliation" / "candidate-snapshot.json"
        ),
        "candidate_review_sha256": sha256_file(package / "reconciliation" / "candidate_review.csv"),
        **candidate_summary,
        "validation_scope": (
            "Checks schema, complete timestamped window coverage, occurrence-window links, blind-lock integrity, "
            "source media hash, OCR metrics/candidate provenance, one-to-one text labels, temporal overlap, and candidate adjudication. "
            "Split/merge/duplicate text and structural timing remain human-adjudicated. It cannot prove that a reviewer watched "
            "every frame or did not see candidate output before freezing the inventory."
        ),
    }
    return report, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze and validate OCR-00 visual subtitle ground truth.")
    parser.add_argument("command", choices=("freeze-blind", "validate"))
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    package = args.package if args.package.is_absolute() else REPO_ROOT / args.package
    if args.command == "freeze-blind":
        result, errors = freeze_blind(package)
    else:
        result, errors = validate_package(package)
        if not errors:
            (package / "ground-truth-validation.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    output = {
        "status": "failed" if errors else "passed",
        "error_count": len(errors),
        "errors": errors[:25],
        "errors_truncated": len(errors) > 25,
        "result": result,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
