"""Value-free HTTP contract observations for clean-room provenance work."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .redaction import is_secret_key

_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_VIDEO_ID = re.compile(r"^(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)$")
_OPAQUE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{24,}$")


@dataclass(frozen=True, slots=True)
class ContractObservation:
    source_id: str
    provider_id: str
    operation: str
    method: str
    url_pattern: str
    status: int
    resource_type: str
    content_type: str
    query_names: tuple[str, ...]
    request_header_names: tuple[str, ...]
    response_header_names: tuple[str, ...]
    json_shape: Any | None
    observed_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "cbce.contract-observation.v1",
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "operation": self.operation,
            "method": self.method,
            "url_pattern": self.url_pattern,
            "status": self.status,
            "resource_type": self.resource_type,
            "content_type": self.content_type,
            "query_names": list(self.query_names),
            "request_header_names": list(self.request_header_names),
            "response_header_names": list(self.response_header_names),
            "json_shape": self.json_shape,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
        }


def sanitize_http_observation(
    *,
    source_id: str,
    provider_id: str,
    operation: str,
    method: str,
    url: str,
    allowed_hosts: tuple[str, ...],
    status: int,
    resource_type: str,
    content_type: str,
    request_headers: Mapping[str, str],
    response_headers: Mapping[str, str],
    json_value: Any | None = None,
    observed_at: datetime | None = None,
) -> ContractObservation:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme not in {"http", "https"} or not _allowed_host(host, allowed_hosts):
        raise ValueError("Observation URL is outside the approved source domains")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Observation URL cannot contain user information")
    try:
        if parsed.port not in {None, 80, 443}:
            raise ValueError("Observation URL uses an unsupported port")
    except ValueError as exc:
        raise ValueError("Observation URL has an invalid or unsupported port") from exc
    query_names = tuple(
        sorted(
            {
                _safe_name(key)
                for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
            }
        )
    )
    path = "/".join(_path_segment(segment) for segment in parsed.path.split("/"))
    pattern = urlunsplit(("https", host, path, "", ""))
    return ContractObservation(
        source_id=source_id.strip().casefold(),
        provider_id=provider_id.strip(),
        operation=operation.strip(),
        method=method.strip().upper(),
        url_pattern=pattern,
        status=int(status),
        resource_type=resource_type.strip()[:40],
        content_type=content_type.split(";", 1)[0].strip().casefold()[:100],
        query_names=query_names,
        request_header_names=_header_names(request_headers),
        response_header_names=_header_names(response_headers),
        json_shape=_json_shape(json_value) if json_value is not None else None,
        observed_at=observed_at or datetime.now(UTC),
    )


def _allowed_host(host: str, roots: tuple[str, ...]) -> bool:
    return any(host == root or host.endswith(f".{root}") for root in roots)


def _path_segment(segment: str) -> str:
    if not segment:
        return ""
    if segment.isdigit():
        return "{numeric_id}"
    if _VIDEO_ID.fullmatch(segment):
        return "{video_id}"
    if _OPAQUE_SEGMENT.fullmatch(segment):
        return "{opaque}"
    return segment[:100]


def _header_names(headers: Mapping[str, str]) -> tuple[str, ...]:
    names = set()
    for key in headers:
        if is_secret_key(key):
            continue
        normalized = _safe_name(key).casefold()
        if normalized != "redacted_field":
            names.add(normalized)
    return tuple(sorted(names))


def _safe_name(value: object) -> str:
    text = str(value).strip()
    return text if _SAFE_NAME.fullmatch(text) and not is_secret_key(text) else "redacted_field"


def _json_shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "depth_limit"
    if isinstance(value, Mapping):
        items = list(value.items())[:200]
        return {
            _safe_name(key): _json_shape(item, depth=depth + 1)
            for key, item in items
        }
    if isinstance(value, list):
        if not value:
            return []
        shapes = []
        for item in value[:5]:
            shape = _json_shape(item, depth=depth + 1)
            if shape not in shapes:
                shapes.append(shape)
        return shapes
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "unknown"
