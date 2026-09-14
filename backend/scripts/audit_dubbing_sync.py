"""Capture a recoverable, read-only voice baseline without loading audio models."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import wave
from datetime import UTC, datetime
from pathlib import Path


def capture(document_path: Path, output: Path, sample_count: int = 40) -> dict:
    raw = document_path.read_bytes()
    document = json.loads(raw)
    assets = document_path.parent.parent / "assets"
    rows, manifest = [], {}
    maximum_end = float("-inf")
    for clip in sorted(document["clips"], key=lambda c: c["start_ms"] + c["offset_ms"]):
        identifier = clip.get("asset_id")
        present = False
        if identifier:
            if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                raise ValueError("Invalid asset identifier")
            source = assets / f"{identifier}.wav"
            present = source.is_file()
            if identifier not in manifest:
                entry = {"path": str(source.resolve()), "present": present}
                if present:
                    entry["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                    entry["bytes"] = source.stat().st_size
                    with wave.open(str(source), "rb") as audio:
                        entry["measured_duration_ms"] = audio.getnframes() * 1000 / audio.getframerate()
                manifest[identifier] = entry
        start = clip["start_ms"] + clip["offset_ms"]
        duration = manifest[identifier]["measured_duration_ms"] if present else clip["duration_ms"]
        end = start + duration / clip["rate"] if duration else None
        row = {"id": clip["id"], "asset_id": identifier, "present": present,
               "text": clip["spoken_text"], "cue_start_ms": clip["start_ms"],
               "cue_end_ms": clip["end_ms"], "file_start_ms": start, "file_end_ms": end,
               "duration_ms": duration, "rate": clip["rate"], "offset_ms": clip["offset_ms"],
               "status": clip["status"], "source_alignment": "unverified",
               "outside_cue": bool(present and (start < clip["start_ms"] - 2 or end > clip["end_ms"] + 2)),
               "overlaps_prior": bool(present and start < maximum_end - 2)}
        if present:
            maximum_end = max(maximum_end, end)
        rows.append(row)
    if document_path.read_bytes() != raw:
        raise RuntimeError("Document changed during capture; retry with a stable revision")
    output.mkdir(parents=True, exist_ok=False)
    (output / "document.original.json").write_bytes(raw)
    (output / "samples").mkdir()
    available = [r for r in rows if r["present"]]
    chosen = {}
    # Alternate timing cases, short/long clips and positions. Acoustic categories
    # such as music and speaker turns require listening, never infer them here.
    groups = [
        [r for r in available if "chỗ rách" in r["text"]],
        [r for r in available if r["overlaps_prior"]],
        [r for r in available if r["outside_cue"]],
        sorted(available, key=lambda r: r["duration_ms"]),
        sorted(available, key=lambda r: -r["duration_ms"]),
        [r for r in available if "," in r["text"]],
        available[::max(1, len(available) // max(1, sample_count))],
        list(reversed(available)),
    ]
    for index, group in enumerate(groups):
        if index not in (0, 3, 4):
            group[:] = group[::max(1, len(group) // max(1, sample_count))]
    while len(chosen) < min(sample_count, len(available)):
        before = len(chosen)
        for group in groups:
            while group and group[0]["id"] in chosen:
                group.pop(0)
            if group and len(chosen) < sample_count:
                row = group.pop(0)
                chosen[row["id"]] = row
        if len(chosen) == before:
            for row in available:
                if len(chosen) >= sample_count:
                    break
                chosen.setdefault(row["id"], row)
            break
    cards = []
    for row in sorted(chosen.values(), key=lambda r: r["cue_start_ms"]):
        filename = f'{row["asset_id"]}.wav'
        target = output / "samples" / filename
        if not target.exists():
            shutil.copyfile(assets / filename, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != manifest[row["asset_id"]]["sha256"]:
            raise RuntimeError("Asset changed during capture")
        cards.append(f'<article><h3>{row["cue_start_ms"] / 1000:.3f}s · {html.escape(row["text"])}</h3>'
                     f'<p>Original WAV · rate in project: {row["rate"]} · offset: {row["offset_ms"]} ms</p>'
                     f'<audio controls preload="none" src="samples/{filename}"></audio></article>')
    (output / "listen.html").write_text('<!doctype html><meta charset="utf-8"><title>Dubbing baseline</title>'
        '<h1>Original audio samples</h1><p>Source speech boundaries are not verified. '
        'These players play raw WAVs at 1x, not the project mix.</p>' + "\n".join(cards), encoding="utf-8")
    with (output / "timing.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["id"])
        writer.writeheader()
        writer.writerows(rows)
    summary = {"captured_utc": datetime.now(UTC).isoformat(),
               "source_document": str(document_path.resolve()), "revision": document["revision"],
               "document_sha256": hashlib.sha256(raw).hexdigest(), "clips": len(rows),
               "audio_available": len(available), "outside_cue": sum(r["outside_cue"] for r in rows),
               "overlapping_intervals": sum(r["overlaps_prior"] for r in rows),
               "samples": len(chosen), "source_alignment_verified": 0,
               "recovery": "Restore document.original.json with app stopped; asset manifest points to retained originals."}
    for name, value in (("audit.json", summary), ("assets.json", manifest),
                        ("sample-review.json", [{**r, "source_speech_start_ms": None,
                         "source_speech_end_ms": None, "review_notes": "pending source listening"}
                         for r in chosen.values()])):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("output", type=Path, help="New directory; existing directories are never overwritten")
    args = parser.parse_args()
    print(json.dumps(capture(args.document, args.output), ensure_ascii=True))
