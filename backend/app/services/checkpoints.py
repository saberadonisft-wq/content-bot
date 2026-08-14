from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_CODEC_VERSION = 1
DEFAULT_RECENT_ID_LIMIT = 1_000
MAX_CURSOR_BYTES = 16_384
_CURSOR_KEY_PREFIX = "scope_"


def _normalized_text(value: object, *, casefold: bool = False) -> str:
    text = " ".join(str(value).strip().split())
    return text.casefold() if casefold else text


def _json_value(value: Any) -> Any:
    """Return a deterministic, JSON-compatible copy or raise for unsafe input."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Checkpoint values cannot contain NaN or infinity")
        return value
    if isinstance(value, datetime):
        return _datetime_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if not key:
                raise ValueError("Checkpoint object keys cannot be empty")
            result[key] = _json_value(raw_value)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_value(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    raise TypeError(f"Unsupported checkpoint value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def query_fingerprint(
    *,
    source_id: str,
    provider: str,
    operation: str,
    terms: Iterable[str] = (),
    target: Any = None,
    filters: Mapping[str, Any] | None = None,
    provider_version: str = "",
    codec_version: int = CHECKPOINT_CODEC_VERSION,
) -> str:
    """Fingerprint the query semantics that make provider cursors reusable.

    Terms are whitespace-normalized, case-folded, and de-duplicated while
    retaining their order. Runtime-only budgets and credentials are omitted by
    design; callers should put every cursor-relevant option in ``filters``.
    """
    normalized_terms: list[str] = []
    seen_terms: set[str] = set()
    for term in terms:
        normalized = _normalized_text(term, casefold=True)
        if normalized and normalized not in seen_terms:
            seen_terms.add(normalized)
            normalized_terms.append(normalized)

    document = {
        "source_id": _required_identifier(source_id, "source_id"),
        "provider": _required_identifier(provider, "provider"),
        "operation": _required_identifier(operation, "operation"),
        "terms": normalized_terms,
        "target": _json_value(target),
        "filters": _json_value(filters or {}),
        "provider_version": _normalized_text(provider_version),
        "codec_version": int(codec_version),
    }
    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def cursor_scope(kind: str, **dimensions: Any) -> dict[str, Any]:
    """Build the canonical scope whose hash is safe as a Mongo field name."""
    result: dict[str, Any] = {"kind": _required_identifier(kind, "scope kind")}
    for raw_key, raw_value in dimensions.items():
        key = _required_identifier(raw_key, "scope dimension")
        if raw_value is None:
            continue
        if isinstance(raw_value, str):
            value: Any = _normalized_text(
                raw_value,
                casefold=key in {"term", "hashtag"},
            )
        else:
            value = _json_value(raw_value)
        if value != "":
            result[key] = value
    return result


def cursor_scope_key(scope: Mapping[str, Any]) -> str:
    canonical_scope = _canonical_scope(scope)
    digest = hashlib.sha256(_canonical_json(canonical_scope).encode("utf-8")).hexdigest()
    return f"{_CURSOR_KEY_PREFIX}{digest}"


class CheckpointTracker:
    """Stage cursor/watermark changes until the caller durably commits them.

    The tracker never performs I/O. A run can call ``candidate()`` after all
    emitted records are persisted, write that envelope to storage, and then call
    ``commit_candidate()``. On failure, ``discard_candidate()`` restores the
    last committed in-memory state.
    """

    def __init__(
        self,
        checkpoint: Mapping[str, Any] | None,
        *,
        source_id: str,
        provider: str,
        operation: str,
        query_fingerprint: str,
        recent_id_limit: int = DEFAULT_RECENT_ID_LIMIT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if recent_id_limit < 0:
            raise ValueError("recent_id_limit must be non-negative")
        if len(query_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in query_fingerprint
        ):
            raise ValueError("query_fingerprint must be a lowercase SHA-256 hex digest")

        self.source_id = _required_identifier(source_id, "source_id")
        self.provider = _required_identifier(provider, "provider")
        self.operation = _required_identifier(operation, "operation")
        self.query_fingerprint = query_fingerprint
        self.recent_id_limit = recent_id_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self.loaded_compatible = self._is_compatible(checkpoint)
        self._committed = self._load(checkpoint if self.loaded_compatible else None)
        self._staged = deepcopy(self._committed)

    @property
    def dirty(self) -> bool:
        return self._comparable(self._staged) != self._comparable(self._committed)

    @property
    def latest_published_at(self) -> datetime | None:
        return _parse_datetime(self._staged.get("latest_published_at"))

    @property
    def recent_ids(self) -> tuple[str, ...]:
        return tuple(self._staged["recent_ids"])

    def cursor(self, scope: Mapping[str, Any], default: Any = None) -> Any:
        canonical_scope = _canonical_scope(scope)
        entry = self._staged["provider_cursors"].get(cursor_scope_key(canonical_scope))
        if not isinstance(entry, Mapping) or entry.get("scope") != canonical_scope:
            return deepcopy(default)
        return deepcopy(entry.get("value"))

    def report_cursor(self, scope: Mapping[str, Any], value: Any | None) -> None:
        canonical_scope = _canonical_scope(scope)
        key = cursor_scope_key(canonical_scope)
        if value is None:
            self._staged["provider_cursors"].pop(key, None)
            return
        safe_value = _json_value(value)
        if len(_canonical_json(safe_value).encode("utf-8")) > MAX_CURSOR_BYTES:
            raise ValueError(f"Provider cursor exceeds {MAX_CURSOR_BYTES} bytes")
        self._staged["provider_cursors"][key] = {
            "scope": canonical_scope,
            "value": safe_value,
        }

    def observe(
        self,
        external_id: object,
        published_at: datetime | str | None = None,
    ) -> None:
        normalized_id = str(external_id).strip()
        if normalized_id and self.recent_id_limit:
            previous = self._staged["recent_ids"]
            self._staged["recent_ids"] = [
                normalized_id,
                *(item for item in previous if item != normalized_id),
            ][: self.recent_id_limit]

        if published_at is None:
            return
        observed = _parse_datetime(published_at, strict=True)
        latest = _parse_datetime(self._staged.get("latest_published_at"))
        if latest is None or observed > latest:
            self._staged["latest_published_at"] = _datetime_text(observed)

    def committed(self) -> dict[str, Any]:
        return deepcopy(self._committed)

    def candidate(self, *, observed_at: datetime | None = None) -> dict[str, Any]:
        result = deepcopy(self._staged)
        result["observed_at"] = _datetime_text(observed_at or self._clock())
        return result

    def commit_candidate(
        self,
        candidate: Mapping[str, Any] | None = None,
        *,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Mark a candidate as committed after its external storage write succeeds."""
        next_checkpoint = (
            self.candidate(observed_at=observed_at)
            if candidate is None
            else self._load(candidate)
        )
        if not self._is_compatible(next_checkpoint):
            raise ValueError("Candidate checkpoint is incompatible with this tracker")
        self._committed = deepcopy(next_checkpoint)
        self._staged = deepcopy(next_checkpoint)
        return deepcopy(next_checkpoint)

    def discard_candidate(self) -> None:
        self._staged = deepcopy(self._committed)

    def _is_compatible(self, checkpoint: Mapping[str, Any] | None) -> bool:
        if not isinstance(checkpoint, Mapping):
            return False
        return (
            checkpoint.get("schema_version") == CHECKPOINT_SCHEMA_VERSION
            and checkpoint.get("codec_version") == CHECKPOINT_CODEC_VERSION
            and checkpoint.get("source_id") == self.source_id
            and checkpoint.get("provider") == self.provider
            and checkpoint.get("operation") == self.operation
            and checkpoint.get("query_fingerprint") == self.query_fingerprint
        )

    def _load(self, checkpoint: Mapping[str, Any] | None) -> dict[str, Any]:
        result = self._empty()
        if not isinstance(checkpoint, Mapping):
            return result

        cursors = checkpoint.get("provider_cursors")
        if isinstance(cursors, Mapping):
            for raw_key, raw_entry in cursors.items():
                if not isinstance(raw_key, str) or not isinstance(raw_entry, Mapping):
                    continue
                scope = raw_entry.get("scope")
                if not isinstance(scope, Mapping):
                    continue
                try:
                    canonical_scope = _canonical_scope(scope)
                    expected_key = cursor_scope_key(canonical_scope)
                    value = _json_value(raw_entry.get("value"))
                except (TypeError, ValueError):
                    continue
                if raw_key != expected_key:
                    continue
                if len(_canonical_json(value).encode("utf-8")) > MAX_CURSOR_BYTES:
                    continue
                result["provider_cursors"][raw_key] = {
                    "scope": canonical_scope,
                    "value": value,
                }

        result["recent_ids"] = _bounded_recent_ids(
            checkpoint.get("recent_ids"), self.recent_id_limit
        )
        latest = _parse_datetime(checkpoint.get("latest_published_at"))
        if latest is not None:
            result["latest_published_at"] = _datetime_text(latest)
        observed = _parse_datetime(checkpoint.get("observed_at"))
        if observed is not None:
            result["observed_at"] = _datetime_text(observed)
        return result

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "codec_version": CHECKPOINT_CODEC_VERSION,
            "source_id": self.source_id,
            "provider": self.provider,
            "operation": self.operation,
            "query_fingerprint": self.query_fingerprint,
            "provider_cursors": {},
            "recent_ids": [],
            "latest_published_at": None,
            "observed_at": None,
        }

    @staticmethod
    def _comparable(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(checkpoint))
        result.pop("observed_at", None)
        return result


def _required_identifier(value: object, label: str) -> str:
    normalized = _normalized_text(value, casefold=True)
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    return normalized


def _canonical_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(scope, Mapping):
        raise TypeError("Cursor scope must be a mapping")
    kind = scope.get("kind")
    dimensions = {str(key): value for key, value in scope.items() if key != "kind"}
    return cursor_scope(str(kind or ""), **dimensions)


def _bounded_recent_ids(value: Any, limit: int) -> list[str]:
    if limit == 0 or not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in value:
        if isinstance(raw_item, (Mapping, list, tuple, set, frozenset)):
            continue
        item = str(raw_item).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _parse_datetime(
    value: datetime | str | Any,
    *,
    strict: bool = False,
) -> datetime | None:
    if not value:
        if strict:
            raise ValueError("published_at cannot be empty")
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError("published_at must be an ISO-8601 datetime") from exc
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _datetime_text(value: datetime) -> str:
    parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
