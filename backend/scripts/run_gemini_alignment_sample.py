"""Measure optional alignment on flagged sample cues using an existing local model."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.media_probe import probe_media
from app.services.subtitle_alignment import AlignmentSettings, align_subtitle_document


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", type=Path)
    args = parser.parse_args()
    source = json.loads((args.sample / "result.json").read_text(encoding="utf-8"))
    selected = {cue["id"] for cue in source["document"]["segments"] if cue.get("needs_review")}
    started = time.monotonic()
    result = align_subtitle_document(args.sample / "sample.mp4", source["document"], probe_media(args.sample / "sample.mp4"),
        settings=AlignmentSettings(engine="faster_whisper", whisper_device="cpu", whisper_compute_type="int8", whisper_allow_download=False),
        cue_ids=selected, progress=lambda percent, phase, message: print(percent, phase, message, flush=True))
    result["processing_seconds"] = round(time.monotonic() - started, 3)
    result["selected_ids"] = sorted(selected)
    (args.sample / "alignment-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Aligned", result["aligned_cue_count"], "warnings", len(result["warnings"]), "seconds", result["processing_seconds"])


if __name__ == "__main__":
    main()
