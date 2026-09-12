from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from .models import SDK_VERSION, V2_MODEL_ID, VoiceDocument


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalized_text(text: str, pronunciation: dict[str, str]) -> str:
    text = unicodedata.normalize("NFC", text)
    for word, replacement in sorted(
        pronunciation.items(), key=lambda row: -len(row[0])
    ):
        text = re.sub(
            r"(?<!\w)" + re.escape(word) + r"(?!\w)",
            lambda _, value=replacement: value,
            text,
            flags=re.IGNORECASE,
        )
    return " ".join(text.split())


def generation_hash(document: VoiceDocument, clip, device: str) -> str:
    profile = document.profile.model_dump()
    # Keep existing v3 audio valid: old profiles implicitly enabled denoising.
    if profile["denoise"] or not profile["reference_id"]:
        profile.pop("denoise")
    return digest(
        {
            "pipeline": 1,
            "sdk": SDK_VERSION,
            "profile": profile,
            "text": normalized_text(clip.spoken_text, document.pronunciation),
            "backend": device,
            "precision": "bf16" if document.profile.model_id == V2_MODEL_ID else "fp32",
            "temperature": 0.4 if document.profile.model_id == V2_MODEL_ID else 0.8,
        }
    )


class VoiceStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()

    def owner_root(self, owner: str) -> Path:
        return self.root / hashlib.sha256(owner.encode()).hexdigest()[:24]

    def path(self, owner: str, kind: str, identifier: str, suffix=".json") -> Path:
        if kind not in {
            "projects",
            "profiles",
            "assets",
            "jobs",
            "references",
            "packages",
        }:
            raise ValueError("Invalid asset kind")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identifier):
            raise ValueError("Invalid identifier")
        return self.owner_root(owner) / kind / (identifier + suffix)

    def get_document(self, owner: str, project: str) -> VoiceDocument:
        return VoiceDocument.model_validate(
            read_json(self.path(owner, "projects", project))
        )

    def save_document(self, owner: str, document: VoiceDocument) -> VoiceDocument:
        with self.lock:
            path = self.path(owner, "projects", document.project_id)
            old = (
                self.get_document(owner, document.project_id) if path.exists() else None
            )
            if old and document.revision != old.revision:
                raise ValueError("Dự án vừa thay đổi. Tải lại trước khi lưu.")
            if old and old.video_fingerprint != document.video_fingerprint:
                raise ValueError("Video của dự án không khớp.")
            if (
                document.profile.reference_id
                and not self.path(
                    owner, "references", document.profile.reference_id, ".wav"
                ).is_file()
            ):
                raise ValueError("Không tìm thấy mẫu giọng.")
            # Never trust client asset metadata: preserve only server-owned verified assets.
            for clip in document.clips:
                if clip.asset_id:
                    meta_path = self.path(owner, "assets", clip.asset_id)
                    if not meta_path.exists():
                        raise ValueError("Không tìm thấy audio của đoạn.")
                    meta = read_json(meta_path)
                    clip.duration_ms = meta["duration_ms"]
                    clip.generation_hash = meta["generation_hash"]
                    expected = generation_hash(document, clip, meta["device"])
                    clip.status = (
                        "ready" if expected == clip.generation_hash else "stale"
                    )
                    if (
                        clip.status == "ready"
                        and clip.duration_ms / clip.rate > clip.end_ms - clip.start_ms
                    ):
                        clip.status = "overflow"
            document.revision += 1
            write_json(path, document.model_dump())
            return document

    def attach(self, owner: str, project: str, clip_id: str, asset: dict) -> bool:
        with self.lock:
            doc = self.get_document(owner, project)
            clip = next((c for c in doc.clips if c.id == clip_id), None)
            if (
                not clip
                or generation_hash(doc, clip, asset["device"])
                != asset["generation_hash"]
            ):
                return False
            if (
                clip.asset_id == asset["id"]
                and clip.generation_hash == asset["generation_hash"]
            ):
                return True
            clip.asset_id = asset["id"]
            clip.generation_hash = asset["generation_hash"]
            clip.duration_ms = asset["duration_ms"]
            # Limit automatic fitting to 1.20x total. Longer lines require review.
            needed = clip.duration_ms / (clip.end_ms - clip.start_ms)
            clip.rate = max(clip.rate, min(1.20, needed))
            clip.status = "overflow" if needed > clip.rate + 0.001 else "ready"
            clip.error = None
            doc.revision += 1
            write_json(self.path(owner, "projects", project), doc.model_dump())
            return True

    def mark_failed(self, owner: str, project: str, clip_id: str, expected_hash: str, device: str, error: str) -> bool:
        with self.lock:
            doc = self.get_document(owner, project)
            clip = next((c for c in doc.clips if c.id == clip_id), None)
            if not clip or generation_hash(doc, clip, device) != expected_hash:
                return False
            if clip.status == "ready" or (clip.status == "failed" and clip.error == error):
                return False
            clip.status = "failed"
            clip.error = error[:1000]
            doc.revision += 1
            write_json(self.path(owner, "projects", project), doc.model_dump())
            return True
