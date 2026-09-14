"""Durable subtitle versions; generated alternatives never replace the editor draft."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..schemas import SubtitleDocumentV2
from .gemini_media import atomic_json, digest_json

_lock = threading.RLock()


class SubtitleVersionStore:
    def __init__(self, root: Path):
        self.root = root

    def directory(self, video_id):
        if not re.fullmatch(r"[a-f0-9]{12,32}", video_id):
            raise ValueError("Video ID không hợp lệ.")
        return self.root / video_id

    def save(self, video_id, document, *, name="Bản phụ đề", source="manual", model=None):
        document = SubtitleDocumentV2.model_validate(document).model_dump(mode="json")
        content_hash = digest_json(document)
        version_id = content_hash[:32]
        record = {"version": 1, "id": version_id, "video_id": video_id, "document": document,
                  "name": name[:100], "source": source, "model": model,
                  "created_at": datetime.now(UTC).isoformat(), "content_hash": content_hash,
                  "cue_count": len(document["segments"])}
        with _lock:
            path = self.directory(video_id) / f"{version_id}.json"
            if path.exists():
                return self.load(video_id, version_id)
            atomic_json(path, record)
        return record

    def load(self, video_id, version_id):
        if not re.fullmatch(r"[a-f0-9]{32}", version_id):
            raise ValueError("Version ID không hợp lệ.")
        with _lock:
            record = json.loads((self.directory(video_id) / f"{version_id}.json").read_text(encoding="utf-8"))
            if record.get("version") != 1 or record.get("video_id") != video_id or record.get("id") != version_id:
                raise ValueError("Phiên bản phụ đề không hợp lệ.")
            return record

    def list(self, video_id, *, offset=0, limit=20):
        directory = self.directory(video_id)
        with _lock:
            paths = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
            records = []
            for path in paths[offset:offset + limit]:
                record = self.load(video_id, path.stem)
                records.append({k: v for k, v in record.items() if k != "document"})
            return {"versions": records, "total": len(paths), "offset": offset, "limit": limit}


def regeneration_options(options: dict) -> dict:
    return {**options, "run_id": uuid.uuid4().hex}
