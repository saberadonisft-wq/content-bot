"""Owner-scoped, checksum-verified storage for separated audio stems."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
import wave
from pathlib import Path

from ..gemini_media import atomic_json
from .models import VoiceDocument
from .store import digest, read_json

STEM_ID = re.compile(r"^[a-f0-9]{64}$")
STEM_NAMES = frozenset({"background", "vocals"})
SEPARATION_CACHE_VERSION = "separation-cache-v1"
MAX_ENTRIES = 64
MAX_BYTES = 4 * 1024 * 1024 * 1024


class SeparationStoreError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
    return hasher.hexdigest()


def stem_metadata(path: Path) -> dict:
    checksum = sha256_file(path)
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.getnframes()
        if channels not in {1, 2} or sample_width != 2 or rate <= 0 or frames <= 0:
            raise SeparationStoreError("Stem phải là WAV PCM16 mono hoặc stereo có tín hiệu.")
        peak = 0
        while block := audio.readframes(rate):
            values = memoryview(block).cast("h")
            peak = max(peak, max((abs(value) for value in values), default=0))
        if peak == 0:
            raise SeparationStoreError("Stem tách nền không có tín hiệu.")
    return {"checksum": checksum, "duration_ms": round(frames * 1000 / rate),
            "sample_rate": rate, "channels": channels, "frames": frames,
            "peak": peak / 32768}


class SeparationStore:
    def __init__(self, root: Path, *, max_entries: int = MAX_ENTRIES,
                 max_bytes: int = MAX_BYTES):
        self.root = Path(root).resolve()
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        if max_entries <= 0 or max_bytes <= 0:
            raise ValueError("Hạn mức cache tách nền phải lớn hơn 0.")

    def owner_root(self, owner: str) -> Path:
        return self.root / hashlib.sha256(owner.encode()).hexdigest()[:24]

    def _directory(self, owner: str) -> Path:
        path = self.owner_root(owner) / "separations"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _validate_id(self, stem_id: str) -> str:
        if not STEM_ID.fullmatch(stem_id):
            raise SeparationStoreError("ID stem tách nền không hợp lệ.")
        return stem_id

    def _manifest_path(self, owner: str, stem_id: str) -> Path:
        return self._directory(owner) / f"{self._validate_id(stem_id)}.json"

    def _validate_manifest(self, owner: str, manifest: dict) -> dict:
        stem_id = self._validate_id(str(manifest.get("id", "")))
        if manifest.get("version") != SEPARATION_CACHE_VERSION:
            raise SeparationStoreError("Phiên bản cache tách nền không được hỗ trợ.")
        stems = manifest.get("stems")
        if not isinstance(stems, dict) or "background" not in stems:
            raise SeparationStoreError("Cache tách nền thiếu stem background.")
        directory = self._directory(owner) / stem_id
        if not directory.is_dir() or directory.is_symlink():
            raise SeparationStoreError("Thư mục stem tách nền không tồn tại.")
        checked = dict(manifest)
        for name, value in stems.items():
            if name not in STEM_NAMES or not isinstance(value, dict):
                raise SeparationStoreError("Tên stem tách nền không hợp lệ.")
            filename = value.get("filename")
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise SeparationStoreError("Tên file stem không hợp lệ.")
            path = (directory / filename).resolve()
            if path.parent != directory.resolve() or path.is_symlink() or not path.is_file():
                raise SeparationStoreError("File stem tách nền không hợp lệ.")
            checksum = value.get("checksum")
            if checksum != sha256_file(path):
                raise SeparationStoreError("Checksum stem tách nền không khớp.")
            # audio_metadata also confirms WAV PCM16 mono, the project audio contract.
            metadata = stem_metadata(path)
            if metadata["checksum"] != checksum:
                raise SeparationStoreError("Metadata stem tách nền không khớp.")
            checked["stems"] = {
                **checked.get("stems", {}),
                name: {**value, "metadata": metadata},
            }
        return checked

    def get(self, owner: str, stem_id: str) -> dict:
        path = self._manifest_path(owner, stem_id)
        try:
            manifest = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SeparationStoreError("Không tìm thấy stem tách nền.") from exc
        if not isinstance(manifest, dict):
            raise SeparationStoreError("Manifest stem tách nền không hợp lệ.")
        checked = self._validate_manifest(owner, manifest)
        path.touch()
        return checked

    def file(self, owner: str, stem_id: str, kind: str = "background") -> Path:
        if kind not in STEM_NAMES:
            raise SeparationStoreError("Loại stem không hợp lệ.")
        manifest = self.get(owner, stem_id)
        value = manifest["stems"].get(kind)
        if not value:
            raise SeparationStoreError("Stem được yêu cầu không tồn tại.")
        return (self._directory(owner) / manifest["id"] / value["filename"]).resolve()

    def find_cached(self, owner: str, *, cache_key: str) -> dict | None:
        directory = self._directory(owner)
        for manifest_path in directory.glob("[a-f0-9]*.json"):
            try:
                manifest = read_json(manifest_path)
                if manifest.get("cache_key") == cache_key:
                    return self._validate_manifest(owner, manifest)
            except (OSError, ValueError, json.JSONDecodeError, SeparationStoreError):
                continue
        return None

    def _trim(self, owner: str, protected: set[str] | None = None) -> None:
        directory = self._directory(owner)
        protected = protected or set()
        rows = []
        for manifest_path in directory.glob("[a-f0-9]*.json"):
            stem_id = manifest_path.stem
            if not STEM_ID.fullmatch(stem_id):
                continue
            folder = directory / stem_id
            size = sum(path.stat().st_size for path in folder.glob("*") if path.is_file()) if folder.is_dir() else 0
            rows.append((manifest_path.stat().st_mtime_ns, stem_id, size))
        total = 0
        for _, stem_id, size in rows:
            manifest_path = directory / f"{stem_id}.json"
            total += size + (manifest_path.stat().st_size if manifest_path.exists() else 0)
        count = len(rows)
        for _, stem_id, size in sorted(rows):
            if total <= self.max_bytes and count <= self.max_entries:
                break
            if stem_id in protected:
                continue
            manifest_path = directory / f"{stem_id}.json"
            manifest_size = manifest_path.stat().st_size if manifest_path.exists() else 0
            shutil.rmtree(directory / stem_id, ignore_errors=True)
            manifest_path.unlink(missing_ok=True)
            total -= size + manifest_size
            count -= 1

    def publish(self, owner: str, *, source_fingerprint: str, source_audio_checksum: str,
                cache_key: str, result: dict) -> dict:
        stems = result.get("stems") if isinstance(result, dict) else None
        if not isinstance(stems, dict) or "background" not in stems:
            raise SeparationStoreError("Worker không trả stem background.")
        input_paths = {}
        for name, value in stems.items():
            if name not in STEM_NAMES:
                raise SeparationStoreError("Worker trả stem không hỗ trợ.")
            path = Path(value).resolve()
            if not path.is_file() or path.is_symlink():
                raise SeparationStoreError("Worker trả đường dẫn stem không tồn tại.")
            input_paths[name] = path
        stem_id = digest({"version": SEPARATION_CACHE_VERSION, "source": source_fingerprint,
                          "audio": source_audio_checksum, "cache_key": cache_key})
        directory = self._directory(owner)
        existing = self.find_cached(owner, cache_key=cache_key)
        if existing:
            return existing
        temp = directory / f".{stem_id}.{uuid.uuid4().hex}.part"
        temp.mkdir(parents=True, exist_ok=False)
        try:
            manifest_stems = {}
            for name, source in input_paths.items():
                destination = temp / f"{name}.wav"
                shutil.copyfile(source, destination)
                metadata = stem_metadata(destination)
                checksum = metadata["checksum"]
                manifest_stems[name] = {"filename": destination.name, "checksum": checksum,
                                        "metadata": metadata}
            manifest = {
                "id": stem_id, "version": SEPARATION_CACHE_VERSION, "cache_key": cache_key,
                "source_fingerprint": source_fingerprint, "source_audio_checksum": source_audio_checksum,
                "method": result.get("method"), "model": result.get("model"),
                "device": result.get("device"), "warnings": list(result.get("warnings", [])),
                "created_at": time.time(), "stems": manifest_stems,
            }
            final = directory / stem_id
            if final.exists():
                shutil.rmtree(temp, ignore_errors=True)
            else:
                temp.replace(final)
            atomic_json(directory / f"{stem_id}.json", manifest)
            self._trim(owner, self.selected_ids(owner))
            return self.get(owner, stem_id)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def selected_ids(self, owner: str) -> set[str]:
        protected = set()
        projects = self.owner_root(owner) / "projects"
        for path in projects.glob("*.json") if projects.is_dir() else ():
            try:
                doc = VoiceDocument.model_validate(read_json(path))
                if doc.mix.background_stem_id:
                    protected.add(doc.mix.background_stem_id)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return protected
