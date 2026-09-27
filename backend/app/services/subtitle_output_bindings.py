"""Durable publication records for completed subtitle renders."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .gemini_media import atomic_json


def render_video_id(filename: str) -> str:
    match = re.fullmatch(r"subtitled_([a-f0-9]{12,32})(?:_[a-f0-9]{12})?\.mp4", filename)
    if not match:
        raise ValueError("Tên file render không hợp lệ.")
    return match[1]


def _identity(path: Path):
    stat = path.stat()
    if stat.st_size <= 0:
        raise ValueError("File render rỗng.")
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def publish_render(root: Path, filename: str, video_id: str, fingerprint: str) -> None:
    if render_video_id(filename) != video_id or not fingerprint:
        raise ValueError("Kết quả render không khớp video nguồn.")
    output = root / filename
    record = {"version": 1, "filename": filename, "video_id": video_id,
              "media_fingerprint": fingerprint, "output": _identity(output)}
    atomic_json(root / ".publications" / f"{filename}.json", record)


def verify_render(root: Path, filename: str, fingerprint: str) -> None:
    video_id = render_video_id(filename)
    try:
        record = json.loads((root / ".publications" / f"{filename}.json").read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("filename") != filename or record.get("video_id") != video_id
                or record.get("media_fingerprint") != fingerprint
                or record.get("output") != _identity(root / filename)):
            raise ValueError("File render hoặc video nguồn đã thay đổi; hãy xuất video lại.")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Không xác minh được bản render đã lưu; hãy xuất video lại.") from exc


def _bundle_path(root: Path, bundle: str) -> Path:
    if Path(bundle).name != bundle or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\.json", bundle):
        raise ValueError("Tên manifest Shorts không hợp lệ.")
    return root / ".publications" / f"{bundle}.json"


def publish_shorts(root: Path, video_id: str, manifest: str, filenames: list[str], fingerprint: str) -> None:
    if not fingerprint or not filenames or len(set(filenames)) != len(filenames):
        raise ValueError("Không đủ thông tin để công bố Shorts.")
    if manifest not in filenames or any(Path(name).name != name for name in filenames):
        raise ValueError("Danh sách file Shorts không hợp lệ.")
    record = {"version": 1, "video_id": video_id, "manifest": manifest,
              "media_fingerprint": fingerprint,
              "files": {name: _identity(root / name) for name in sorted(filenames)}}
    atomic_json(_bundle_path(root, manifest), record)


def verify_short(root: Path, video_id: str, filename: str, fingerprint: str) -> None:
    try:
        candidates = list((root / ".publications").glob("*.json"))
        for path in candidates:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("version") == 1 and record.get("video_id") == video_id
                    and record.get("media_fingerprint") == fingerprint
                    and filename in record.get("files", {})):
                if record["files"][filename] == _identity(root / filename):
                    return
                raise ValueError("File Shorts đã thay đổi; hãy xuất lại.")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Không xác minh được Shorts đã lưu; hãy xuất lại.") from exc
    raise ValueError("Shorts không thuộc video nguồn hiện tại hoặc chưa được công bố.")
