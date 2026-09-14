"""Run media review against an intentionally corrupted sample without applying it."""
from __future__ import annotations

import json
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.credential_resolver import credential
from app.services.gemini_review import GeminiReviewStore, ReviewScope, run_review
from app.services.gemini_subtitles import GeminiSubtitleService, GeminiSubtitleSettings
from app.services.media_probe import probe_media


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[2] / "artifacts" / "gemini-pipeline"
    sample = root / "new-sample"
    output = root / "review-sample"
    output.mkdir(parents=True, exist_ok=True)
    original = json.loads((sample / "result.json").read_text(encoding="utf-8"))["document"]
    corrupted = deepcopy(original)
    first = corrupted["segments"][0]
    before = deepcopy(first)
    first["text"] = "Hôm nay trời mưa, chúng ta phải đi ngủ."
    duplicate = {**before, "id": "injected-duplicate"}
    corrupted["segments"].insert(1, duplicate)
    removed = corrupted["segments"].pop(2)
    planted = {"wrong_translation": first["id"], "duplicate": duplicate["id"], "omitted": removed,
               "before_first": before, "note": "Injected changes relative to Gemini sample, not a human gold reference."}
    (output / "planted-errors.json").write_text(json.dumps(planted, ensure_ascii=False, indent=2), encoding="utf-8")
    store = GeminiReviewStore(output / "records")
    saved = output / "review-id.txt"
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=output / "jobs"), api_key_provider=lambda: credential("gemini_api_key"))
    if saved.is_file():
        review_id = saved.read_text().strip()
    else:
        record = store.create(corrupted, ReviewScope(mode="range", start_ms=0, end_ms=40_000), probe_media(sample / "sample.mp4"), service.resolve_model(), "a" * 12)
        review_id = record["id"]
        saved.write_text(review_id)
    class Context:
        cancel_event = threading.Event()
        def update(self, progress, phase, message):
            print(progress, phase, message, flush=True)
        def raise_if_canceled(self):
            if self.cancel_event.is_set():
                raise RuntimeError("Canceled")
    started = time.monotonic()
    result = run_review(service, store, review_id, sample / "sample.mp4", Context())
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Proposals", len(result["proposals"]), "seconds", round(time.monotonic() - started, 3))
    for proposal in result["proposals"]:
        print(proposal["issue"], proposal["operation"], proposal["cue_ids"], proposal["reason"])


if __name__ == "__main__":
    main()
