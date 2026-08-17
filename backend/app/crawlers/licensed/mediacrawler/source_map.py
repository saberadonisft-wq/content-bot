"""Validate provenance metadata before a licensed provider can load."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any


class SourceMapError(ValueError):
    """Raised when the licensed source map is missing or unsafe."""


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ENTRY_FIELDS = (
    "upstream_path",
    "local_path",
    "source_commit",
    "sha256",
    "reuse_kind",
    "adaptation_notes",
    "reviewer",
    "reviewed_at",
)


@dataclass(frozen=True, slots=True)
class LicensedSourceMap:
    root: Path
    payload: dict[str, Any]

    @property
    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload["entries"])


def load_source_map(root: Path) -> LicensedSourceMap:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise SourceMapError("Licensed MediaCrawler reuse directory is missing")
    license_path = root / "LICENSE"
    notice_path = root / "NOTICE.md"
    map_path = root / "SOURCE_MAP.json"
    if not license_path.is_file() or not notice_path.is_file():
        raise SourceMapError("Licensed reuse notice or license is missing")
    license_text = license_path.read_text(encoding="utf-8")
    if "NON-COMMERCIAL LEARNING LICENSE 1.1" not in license_text:
        raise SourceMapError("Unexpected licensed reuse license")
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceMapError("Licensed source map is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SourceMapError("Licensed source map must be an object")
    if payload.get("schema_version") != "cbce.licensed-source-map.v1":
        raise SourceMapError("Unsupported licensed source map schema")
    upstream = payload.get("upstream")
    policy = payload.get("project_policy")
    entries = payload.get("entries")
    if not isinstance(upstream, dict) or not isinstance(policy, dict) or not isinstance(entries, list):
        raise SourceMapError("Licensed source map sections are invalid")
    required_upstream = {
        "repository",
        "commit",
        "snapshot_date",
        "license_file",
        "license_name",
        "copyright",
    }
    if not required_upstream <= set(upstream):
        raise SourceMapError("Licensed source map has incomplete upstream provenance")
    if upstream["license_name"] != "NON-COMMERCIAL LEARNING LICENSE 1.1":
        raise SourceMapError("Licensed source map license does not match LICENSE")
    if upstream["license_file"] != "LICENSE":
        raise SourceMapError("Licensed source map points at an unexpected license file")
    upstream_commit = str(upstream["commit"]).strip().casefold()
    if not _COMMIT.fullmatch(upstream_commit):
        raise SourceMapError("Licensed source map upstream commit is invalid")
    try:
        date.fromisoformat(str(upstream["snapshot_date"]))
    except ValueError as exc:
        raise SourceMapError("Licensed source map snapshot date is invalid") from exc
    if policy != {
        "purpose": "non_commercial_learning_and_research",
        "large_scale_crawling": False,
        "challenge_solving": False,
        "proxy_rotation": False,
        "commercial_use": False,
    }:
        raise SourceMapError("Licensed reuse policy is broader than approved scope")
    required_entry_fields = payload.get("required_entry_fields")
    if (
        not isinstance(required_entry_fields, list)
        or tuple(required_entry_fields) != _REQUIRED_ENTRY_FIELDS
    ):
        raise SourceMapError("Licensed source map entry schema is invalid")
    if not entries:
        raise SourceMapError("Licensed source map must contain reviewed entries")
    local_paths: set[str] = set()
    upstream_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not set(_REQUIRED_ENTRY_FIELDS) <= set(entry):
            raise SourceMapError("Licensed source map contains an incomplete entry")
        if not _safe_relative_path(entry["local_path"]):
            raise SourceMapError("Licensed source map local path is unsafe")
        local_path = str(entry["local_path"]).replace("\\", "/")
        upstream_path = str(entry["upstream_path"]).replace("\\", "/")
        if local_path in local_paths or upstream_path in upstream_paths:
            raise SourceMapError("Licensed source map contains duplicate paths")
        local_paths.add(local_path)
        upstream_paths.add(upstream_path)
        local_file = (root / local_path).resolve()
        if not local_file.is_relative_to(root) or not local_file.is_file():
            raise SourceMapError("Licensed source map local file is missing")
        if not _safe_relative_path(upstream_path):
            raise SourceMapError("Licensed source map upstream path is unsafe")
        if str(entry["source_commit"]).strip().casefold() != upstream_commit:
            raise SourceMapError("Licensed source map entry commit does not match upstream")
        expected_digest = str(entry["sha256"]).strip().casefold()
        if not _SHA256.fullmatch(expected_digest):
            raise SourceMapError("Licensed source map entry digest is invalid")
        try:
            with local_file.open("rb") as handle:
                actual_digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError as exc:
            raise SourceMapError("Licensed source map local file cannot be read") from exc
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise SourceMapError("Licensed source map entry digest does not match")
        try:
            reviewed_at = datetime.fromisoformat(str(entry["reviewed_at"]))
        except ValueError as exc:
            raise SourceMapError("Licensed source map review timestamp is invalid") from exc
        if reviewed_at.tzinfo is None:
            raise SourceMapError("Licensed source map review timestamp needs a timezone")
        if reviewed_at.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=5):
            raise SourceMapError("Licensed source map review timestamp is in the future")
    return LicensedSourceMap(root, payload)


def _safe_relative_path(value: object) -> bool:
    path = Path(str(value).replace("\\", "/"))
    return bool(str(value).strip()) and not path.is_absolute() and ".." not in path.parts
