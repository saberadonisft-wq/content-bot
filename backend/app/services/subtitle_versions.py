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


class SubtitleVersionMediaMismatch(ValueError):
    pass


def _version_id(content_hash, video_id, media_fingerprint):
    if media_fingerprint is None:
        return content_hash[:32]  # Preserve legacy immutable IDs.
    return digest_json({"content_hash": content_hash, "video_id": video_id,
                        "media_fingerprint": media_fingerprint})[:32]


class SubtitleVersionStore:
    def __init__(self, root: Path):
        self.root = root

    def directory(self, video_id):
        if not re.fullmatch(r"[a-f0-9]{12,32}", video_id):
            raise ValueError("Video ID không hợp lệ.")
        return self.root / video_id

    def save(self, video_id, document, *, name="Bản phụ đề", source="manual", model=None,
             media_fingerprint=None):
        if media_fingerprint is not None and (
                not isinstance(media_fingerprint, str) or not media_fingerprint or len(media_fingerprint) > 128):
            raise ValueError("Dấu vân tay video không hợp lệ.")
        document = SubtitleDocumentV2.model_validate(document).model_dump(mode="json")
        bound_document = document.get("media_fingerprint")
        if bound_document and media_fingerprint and bound_document != media_fingerprint:
            raise SubtitleVersionMediaMismatch("Phụ đề thuộc video nguồn khác; không thể lưu vào video hiện tại.")
        media_fingerprint = media_fingerprint or bound_document
        if media_fingerprint:
            document["media_fingerprint"] = media_fingerprint
        content_hash = digest_json(document)
        version_id = _version_id(content_hash, video_id, media_fingerprint)
        record = {"version": 2 if media_fingerprint else 1, "id": version_id, "video_id": video_id, "document": document,
                  "name": name[:100], "source": source, "model": model,
                  "created_at": datetime.now(UTC).isoformat(), "content_hash": content_hash,
                  "cue_count": len(document["segments"])}
        if media_fingerprint:
            record["media_fingerprint"] = media_fingerprint
        with _lock:
            path = self.directory(video_id) / f"{version_id}.json"
            if path.exists():
                return self.load(video_id, version_id)
            atomic_json(path, record)
        return record

    def load(self, video_id, version_id, *, media_fingerprint=None):
        if not re.fullmatch(r"[a-f0-9]{32}", version_id):
            raise ValueError("Version ID không hợp lệ.")
        with _lock:
            record = json.loads((self.directory(video_id) / f"{version_id}.json").read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("version") not in (1, 2)
                    or record.get("video_id") != video_id or record.get("id") != version_id):
                raise ValueError("Phiên bản phụ đề không hợp lệ.")
            document = record.get("document")
            if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
                # Corrupt stored JSON follows the store's ValueError contract.
                raise ValueError("Nội dung phiên bản phụ đề không hợp lệ.")  # noqa: TRY004
            SubtitleDocumentV2.model_validate(document)
            # Validate the stored representation, not a schema upgrade with new
            # defaults, so older immutable versions retain their original hash.
            content_hash = digest_json(document)
            bound_media = record.get("media_fingerprint") if record["version"] == 2 else None
            if record["version"] == 2 and (
                    not isinstance(bound_media, str) or not bound_media or len(bound_media) > 128):
                raise ValueError("Phiên bản phụ đề thiếu định danh video nguồn.")
            if (record.get("content_hash") != content_hash
                    or _version_id(content_hash, video_id, bound_media) != version_id
                    or record.get("cue_count") != len(document["segments"])):
                raise ValueError("Nội dung phiên bản phụ đề không khớp mã kiểm tra.")
            if document.get("media_fingerprint") and document["media_fingerprint"] != bound_media:
                raise ValueError("Định danh nguồn trong tài liệu không khớp phiên bản.")
            if media_fingerprint is not None and bound_media is not None and bound_media != media_fingerprint:
                raise SubtitleVersionMediaMismatch("Phiên bản phụ đề thuộc video nguồn khác; hãy chọn bản phù hợp với video hiện tại.")
            return record

    def list(self, video_id, *, offset=0, limit=20):
        directory = self.directory(video_id)
        with _lock:
            paths = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
            records = []
            unreadable_count = 0
            for path in paths[offset:offset + limit]:
                try:
                    record = self.load(video_id, path.stem)
                except (ValueError, OSError):
                    unreadable_count += 1
                    continue
                records.append({k: v for k, v in record.items() if k != "document"})
            return {"versions": records, "total": len(paths), "offset": offset, "limit": limit,
                    "unreadable_count": unreadable_count}


def regeneration_options(options: dict) -> dict:
    return {**options, "run_id": uuid.uuid4().hex}
