"""Aggregate-only source similarity gate for the clean-room crawler cutover."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CLEANROOM_AUDIT_SCHEMA = "cbce.cleanroom-audit.v1"
_SOURCE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx"})
_TOKEN_RE = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*|\d+(?:\.\d+)?|"
    r"===|!==|==|!=|<=|>=|=>|:=|\*\*|//|&&|\|\||"
    r"[^\sA-Za-z0-9_$]"
)
_INLINE_COMMENT_RE = re.compile(r"(?m)(?<!:)//.*$|#.*$")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_RE = re.compile(
    r"(?s)(?:'''|\"\"\").*?(?:'''|\"\"\")|"
    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`"
)
_IMPORT_LINE_RE = re.compile(r"^(?:from\s+\S+\s+)?import\s+", re.IGNORECASE)
_NGRAM_SIZE = 20
_MAX_COMMON_FILES = 4
_MIN_SHARED_NGRAMS = 10
_MIN_PROJECT_CONTAINMENT = 0.18
_MIN_EXACT_LONG_LINES = 3


@dataclass(frozen=True, slots=True)
class _FileSignature:
    relative_path: str
    digest: str
    fingerprints: frozenset[str]
    long_lines: frozenset[str]


def audit_cleanroom_similarity(project_root: Path) -> dict[str, Any]:
    """Compare CBCE-original implementation expressions without source fragments.

    Source-derived modules under the licensed reuse zone have a separate
    license/provenance gate and must not be misreported as clean-room code.
    """
    root = project_root.resolve()
    project_files = _project_files(root)
    vendor_files = _source_files(root / "vendor" / "mediacrawler")
    project_signatures = tuple(_signature(root, path) for path in project_files)
    vendor_signatures = tuple(_signature(root, path) for path in vendor_files)
    project_frequency = _frequency(project_signatures)
    vendor_frequency = _frequency(vendor_signatures)
    vendor_index: dict[str, set[int]] = defaultdict(set)
    for index, signature in enumerate(vendor_signatures):
        for fingerprint in signature.fingerprints:
            if vendor_frequency[fingerprint] <= _MAX_COMMON_FILES:
                vendor_index[fingerprint].add(index)

    suspicious: list[dict[str, Any]] = []
    compared_pairs = 0
    for project_signature in project_signatures:
        candidate_indexes: set[int] = set()
        project_unique = {
            fingerprint
            for fingerprint in project_signature.fingerprints
            if project_frequency[fingerprint] <= _MAX_COMMON_FILES
        }
        for fingerprint in project_unique:
            candidate_indexes.update(vendor_index.get(fingerprint, ()))
        for vendor_index_value in sorted(candidate_indexes):
            vendor_signature = vendor_signatures[vendor_index_value]
            shared = project_unique & vendor_signature.fingerprints
            exact_lines = (
                project_signature.long_lines & vendor_signature.long_lines
            )
            if not shared and not exact_lines:
                continue
            compared_pairs += 1
            containment = len(shared) / max(len(project_unique), 1)
            if (
                project_signature.digest == vendor_signature.digest
                or (
                    len(shared) >= _MIN_SHARED_NGRAMS
                    and containment >= _MIN_PROJECT_CONTAINMENT
                )
                or len(exact_lines) >= _MIN_EXACT_LONG_LINES
            ):
                suspicious.append(
                    {
                        "project_file": project_signature.relative_path,
                        "vendor_file": vendor_signature.relative_path,
                        "shared_ngram_count": len(shared),
                        "project_containment_ppm": round(containment * 1_000_000),
                        "exact_long_line_count": len(exact_lines),
                        "exact_file_digest": (
                            project_signature.digest == vendor_signature.digest
                        ),
                    }
                )
    suspicious.sort(
        key=lambda item: (
            -int(item["exact_file_digest"]),
            -item["project_containment_ppm"],
            -item["shared_ngram_count"],
            item["project_file"],
            item["vendor_file"],
        )
    )
    return {
        "schema_version": CLEANROOM_AUDIT_SCHEMA,
        "checked_at": datetime.now(UTC).isoformat(),
        "project_tree_digest": _tree_digest(project_signatures),
        "vendor_tree_digest": _tree_digest(vendor_signatures),
        "project_file_count": len(project_signatures),
        "vendor_file_count": len(vendor_signatures),
        "candidate_pair_count": compared_pairs,
        "suspicious_pair_count": len(suspicious),
        "suspicious_pairs": suspicious,
        "thresholds": {
            "ngram_size": _NGRAM_SIZE,
            "minimum_shared_ngrams": _MIN_SHARED_NGRAMS,
            "minimum_project_containment_ppm": round(
                _MIN_PROJECT_CONTAINMENT * 1_000_000
            ),
            "minimum_exact_long_lines": _MIN_EXACT_LONG_LINES,
        },
        "source_fragments_emitted": False,
        "excluded_project_roots": ["backend/app/crawlers/licensed/mediacrawler"],
        "scope": "cbce_original_only",
        "passed": bool(project_signatures)
        and bool(vendor_signatures)
        and not suspicious,
    }


def _project_files(root: Path) -> tuple[Path, ...]:
    crawler_root = root / "backend" / "app" / "crawlers"
    candidates = [
        path
        for path in _source_files(crawler_root)
        if "licensed" not in path.relative_to(crawler_root).parts
    ]
    services = root / "backend" / "app" / "services"
    if services.is_dir():
        candidates.extend(
            path
            for path in _source_files(services)
            if not any(
                marker in path.name.casefold()
                for marker in ("subtitle", "gemini")
            )
        )
    api = root / "backend" / "app" / "api"
    if api.is_dir():
        candidates.extend(_source_files(api))
    scripts = root / "backend" / "scripts"
    if scripts.is_dir():
        candidates.extend(
            path
            for path in _source_files(scripts)
            if path.name.startswith(("cbce_", "crawler_", "migrate_source_ids"))
        )
    return tuple(sorted(set(candidates)))


def _source_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in _SOURCE_SUFFIXES
            and not any(
                part.casefold() in {".git", ".venv", "node_modules", "__pycache__"}
                for part in path.parts
            )
        )
    )


def _signature(root: Path, path: Path) -> _FileSignature:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    scrubbed = _STRING_RE.sub(" STRING ", _BLOCK_COMMENT_RE.sub(" ", text))
    scrubbed = _INLINE_COMMENT_RE.sub(" ", scrubbed)
    tokens = tuple(token.casefold() for token in _TOKEN_RE.findall(scrubbed))
    fingerprints = frozenset(
        hashlib.sha256("\x1f".join(tokens[index : index + _NGRAM_SIZE]).encode()).hexdigest()
        for index in range(max(0, len(tokens) - _NGRAM_SIZE + 1))
    )
    long_lines: set[str] = set()
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if (
            len(line) < 80
            or line.startswith(("#", "//", "/*", "*"))
            or _IMPORT_LINE_RE.match(line)
        ):
            continue
        long_lines.add(hashlib.sha256(line.casefold().encode()).hexdigest())
    return _FileSignature(
        relative_path=path.relative_to(root).as_posix(),
        digest=hashlib.sha256(raw).hexdigest(),
        fingerprints=fingerprints,
        long_lines=frozenset(long_lines),
    )


def _frequency(signatures: tuple[_FileSignature, ...]) -> Counter[str]:
    return Counter(
        fingerprint
        for signature in signatures
        for fingerprint in signature.fingerprints
    )


def _tree_digest(signatures: tuple[_FileSignature, ...]) -> str | None:
    if not signatures:
        return None
    value = "\n".join(
        f"{signature.relative_path}\0{signature.digest}"
        for signature in signatures
    )
    return hashlib.sha256(value.encode()).hexdigest()
