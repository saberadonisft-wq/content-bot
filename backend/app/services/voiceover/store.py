from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from ..vietnamese_tts_normalizer import normalize_vietnamese_for_tts
from .models import SDK_VERSION, V2_MODEL_ID, VoiceDocument
from .timing import AUTO_RATE_LIMIT, clip_signature, file_interval, refresh_timing


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
    for attempt in range(10):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            # Windows can briefly deny opens while an atomic checkpoint is replaced.
            if attempt == 9:
                raise
            time.sleep(.01)


def normalized_text(text: str, pronunciation: dict[str, str], normalization: str = 'off') -> str:
    text = unicodedata.normalize("NFC", text)
    if normalization != 'off':
        if normalization != 'vi-context-v1':
            raise ValueError('Phiên bản chuẩn hóa lời đọc không được hỗ trợ.')
        if not pronunciation:
            return normalize_vietnamese_for_tts(text)
        # Longest dictionary key wins. Replacements are literal and do not pass
        # through the normalizer or another dictionary entry a second time.
        keys = sorted((key for key in pronunciation if key.strip()),key=len,reverse=True)
        if not keys:
            return normalize_vietnamese_for_tts(text)
        pattern = re.compile(r'(?<!\w)(?:'+'|'.join(re.escape(key) for key in keys)+r')(?!\w)',re.IGNORECASE)
        def normalize_fragment(fragment):
            if not fragment.strip(): return fragment
            return (' ' if fragment[:1].isspace() else '')+normalize_vietnamese_for_tts(fragment)+(' ' if fragment[-1:].isspace() else '')
        parts=[]; end=0
        for match in pattern.finditer(text):
            parts.append(normalize_fragment(text[end:match.start()]))
            parts.append(next(pronunciation[key] for key in keys if key.casefold()==match.group().casefold()))
            end=match.end()
        parts.append(normalize_fragment(text[end:]))
        return ' '.join(''.join(parts).split())
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
            **({'normalizer':document.text_normalization} if document.text_normalization != 'off' else {}),
            "sdk": SDK_VERSION,
            "profile": profile,
            "text": normalized_text(clip.spoken_text, document.pronunciation, document.text_normalization),
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
        document = VoiceDocument.model_validate(read_json(self.path(owner, "projects", project)))
        self.validate_alignments(owner, document)
        return refresh_timing(document)

    @staticmethod
    def alignment_binding(document: VoiceDocument, clip) -> str:
        return digest({'project': document.project_id, 'video': document.video_fingerprint,
            'profile': document.profile.model_dump(), 'pronunciation': document.pronunciation,
            **({'normalizer':document.text_normalization} if document.text_normalization != 'off' else {}),
            'clip': clip_signature(clip), 'rate': float(clip.rate)})

    def validate_alignments(self, owner: str, document: VoiceDocument) -> None:
        # A client cannot turn a guessed source window into a verified alignment.
        for clip in document.clips:
            alignment = clip.sync.alignment
            if alignment is None:
                continue
            try:
                proof = read_json(self.owner_root(owner) / 'sync-fits' / f'{alignment.proof_id}.json')
                valid = (proof['alignment'] == alignment.model_dump()
                    and proof['binding'] == self.alignment_binding(document, clip))
            except (OSError, ValueError, KeyError):
                valid = False
            if not valid:
                clip.sync.alignment = None

    def _write_document(self, owner: str, document: VoiceDocument) -> None:
        path = self.path(owner, "projects", document.project_id)
        if path.exists():
            raw = path.read_bytes()
            if json.loads(raw).get("schema_version", 1) == 1:
                snapshot = path.parent / "snapshots" / document.project_id / (hashlib.sha256(raw).hexdigest() + ".v1.json")
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                if not snapshot.exists():
                    # Exclusive creation preserves an existing recovery copy.
                    with snapshot.open("xb") as stream:
                        stream.write(raw)
                if snapshot.read_bytes() != raw:
                    raise ValueError("Bản phục hồi không toàn vẹn. Chưa ghi thay đổi dự án.")
        document.schema_version = 2
        self.validate_alignments(owner, document)
        write_json(path, refresh_timing(document).model_dump())
        document._migrated_from_v1 = False

    def save_document(self, owner: str, document: VoiceDocument) -> VoiceDocument:
        with self.lock:
            path = self.path(owner, "projects", document.project_id)
            old = (
                self.get_document(owner, document.project_id) if path.exists() else None
            )
            if old and document.revision != old.revision:
                raise ValueError("Dự án vừa thay đổi. Tải lại trước khi lưu.")
            if old and document._migrated_from_v1 and not old._migrated_from_v1:
                raise ValueError("Dự án đã nâng phiên bản đồng bộ. Tải lại ứng dụng trước khi lưu.")
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
            document.revision += 1
            self._write_document(owner, document)
            return document

    def attach(self, owner: str, project: str, clip_id: str, asset: dict) -> bool:
        return clip_id in self.attach_many(owner, project, {clip_id: asset})

    def attach_many(self, owner: str, project: str, assets: dict[str, dict]) -> set[str]:
        if not assets:
            return set()
        with self.lock:
            doc = self.get_document(owner, project)
            accepted = set()
            changed = False
            for clip in doc.clips:
                asset = assets.get(clip.id)
                if not asset or generation_hash(doc, clip, asset["device"]) != asset["generation_hash"]:
                    continue
                accepted.add(clip.id)
                if clip.asset_id == asset["id"] and clip.generation_hash == asset["generation_hash"]:
                    continue
                clip.asset_id = asset["id"]
                clip.generation_hash = asset["generation_hash"]
                clip.duration_ms = asset["duration_ms"]
                # Keep manual and unclassified legacy adjustments unchanged.
                remaining = clip.end_ms - file_interval(clip)[0]
                if (clip.sync.timing_origin == "automatic" and not clip.sync.timing_locked
                        and remaining > 0 and clip.rate <= AUTO_RATE_LIMIT):
                    needed = clip.duration_ms / remaining
                    clip.rate = max(clip.rate, min(AUTO_RATE_LIMIT, needed))
                clip.status = "ready"
                clip.error = None
                changed = True
            if changed:
                doc.revision += 1
                self._write_document(owner, doc)
            return accepted

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
            self._write_document(owner, doc)
            return True
