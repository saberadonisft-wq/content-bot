from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from .audio import audio_metadata
from .manager import RUNTIME, VoiceManager
from .models import VoiceDocument
from .store import generation_hash, read_json, write_json

MAX_PACKAGE_BYTES = 4 * 1024**3


def export_package(
    manager: VoiceManager, owner: str, project: str, device: str
) -> Path:
    store = manager.store
    document = store.get_document(owner, project)
    manifest = manager.manifest(owner, document, device)
    manifest["export_id"] = uuid.uuid4().hex
    manifest_path = store.path(owner, "packages", manifest["export_id"])
    write_json(manifest_path, manifest)
    dest = manifest_path.with_suffix(".zip")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", manifest_path.read_bytes())
        bundle.write(RUNTIME / "worker.py", "worker.py")
        bundle.write(RUNTIME / "requirements.txt", "requirements.txt")
        bundle.write(RUNTIME / "requirements-gpu.txt", "requirements-gpu.txt")
        notebook = RUNTIME.parents[1] / "notebooks" / "voiceover_kaggle.ipynb"
        bundle.write(notebook, "voiceover_kaggle.ipynb")
        if document.profile.reference_id:
            bundle.write(
                store.path(owner, "references", document.profile.reference_id, ".wav"),
                "reference.wav",
            )
        included_assets = set()
        for item in manifest["clips"]:
            key = item["generation_hash"]
            if key in included_assets:
                continue
            path = store.path(owner, "assets", key, ".wav")
            meta_path = store.path(owner, "assets", key)
            if path.exists() and meta_path.exists():
                metadata = read_json(meta_path)
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != metadata["checksum"]
                ):
                    continue
                bundle.write(path, f"assets/{key}.wav")
                bundle.writestr(f"assets/{key}.json", meta_path.read_bytes())
                included_assets.add(key)
    return dest


def import_package(
    manager: VoiceManager, owner: str, project: str, path: Path
) -> VoiceDocument:
    store = manager.store
    with (
        zipfile.ZipFile(path) as bundle,
        tempfile.TemporaryDirectory(prefix="voice-import-") as tmp,
    ):
        entries = bundle.infolist()
        if (
            len(entries) > 40005
            or sum(i.file_size for i in entries) > MAX_PACKAGE_BYTES
        ):
            raise ValueError("Gói quá lớn sau khi giải nén.")
        names = [i.filename for i in entries]
        if len(names) != len(set(names)):
            raise ValueError("Gói có tên file trùng nhau.")
        for entry in entries:
            if entry.filename not in {
                "manifest.json",
                "progress.json",
                "environment.txt",
            } and not re.fullmatch(r"assets/[a-f0-9]{64}\.(wav|json)", entry.filename):
                raise ValueError(
                    "Gói kết quả có đường dẫn hoặc loại file không hợp lệ."
                )
            limit = 64 * 1024**2 if entry.filename.endswith(".wav") else 16 * 1024**2
            if (
                entry.file_size > limit
                or entry.flag_bits & 1
                or entry.external_attr >> 16 & 0o170000 == 0o120000
            ):
                raise ValueError("File trong gói không hợp lệ hoặc quá lớn.")
        if "manifest.json" not in names:
            raise ValueError("Thiếu manifest kết quả.")
        import json

        incoming = json.loads(bundle.read("manifest.json"))
        original = read_json(
            store.path(owner, "packages", incoming.get("export_id", ""))
        )
        if incoming != original or original["project_id"] != project:
            raise ValueError("Manifest không khớp gói đã xuất từ dự án này.")
        doc = store.get_document(owner, project)
        if doc.video_fingerprint != original["video_fingerprint"]:
            raise ValueError("Kết quả không thuộc video đang mở.")
        staged = []
        validated = {}
        clip_index = {clip.id: clip for clip in doc.clips}
        for item in original["clips"]:
            key = item["generation_hash"]
            filename = f"assets/{key}.wav"
            if filename not in names:
                continue  # Partial results are useful checkpoints.
            clip = clip_index.get(item["id"])
            if not clip or generation_hash(doc, clip, original["device"]) != key:
                continue  # A newer local edit wins.
            if key in validated:
                wav, meta = validated[key]
                staged.append((item["id"], wav, meta))
                continue
            sidecar = f"assets/{key}.json"
            if sidecar not in names:
                raise ValueError("Audio thiếu checksum.")
            wav = Path(tmp) / f"{key}.wav"
            with bundle.open(filename) as src, wav.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            meta = audio_metadata(wav)
            if meta["checksum"] != json.loads(bundle.read(sidecar)).get("checksum"):
                raise ValueError("Checksum audio không khớp.")
            meta.update(id=key, generation_hash=key, device=original["device"])
            validated[key] = (wav, meta)
            staged.append((item["id"], wav, meta))
        # Validate the entire package before writing any result.
        with store.lock:
            committed = set()
            for cid, wav, meta in staged:
                dest = store.path(owner, "assets", meta["id"], ".wav")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if meta["id"] not in committed:
                    shutil.copyfile(wav, dest.with_suffix(".part"))
                    dest.with_suffix(".part").replace(dest)
                    write_json(store.path(owner, "assets", meta["id"]), meta)
                    committed.add(meta["id"])
                store.attach(owner, project, cid, meta)
    return store.get_document(owner, project)
