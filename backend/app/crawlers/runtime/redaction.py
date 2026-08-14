"""Recursive secret redaction for worker diagnostics and event payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:authorization|cookies?|set_cookies?|password|passwd|secrets?|access_tokens?|refresh_tokens?|api_keys?|sessions?|signatures?|qr_data|csrf|xsrf)(?:$|_)",
    re.IGNORECASE,
)
_SAFE_REFERENCE_KEY = re.compile(r"(?:^|_)(?:credential|secret|token|profile)_ref$", re.IGNORECASE)
_HEADER_SECRET = re.compile(
    r"(?im)\b(authorization|cookie|set-cookie|x-api-key)\s*:\s*[^\r\n]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
_QUERY_SECRET = re.compile(
    r"(?i)(access_token|refresh_token|api_key|apikey|token|signature|sig|key)="
)
_QR_DATA = re.compile(r"(?i)(?:data:image/[^;]+;base64,|otpauth://)[^\s]+")
_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def is_secret_key(key: object) -> bool:
    normalized = str(key).strip().replace("-", "_")
    return bool(_SECRET_KEY.search(normalized)) and not bool(
        _SAFE_REFERENCE_KEY.search(normalized)
    )


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if is_secret_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    text = _HEADER_SECRET.sub(lambda match: f"{match.group(1)}: {REDACTED}", value)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _QR_DATA.sub(REDACTED, text)
    text = _URL.sub(lambda match: _redact_url(match.group(0)), text)
    return text


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        query.append((key, REDACTED if _QUERY_SECRET.search(f"{key}=") else item))
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment)
    ).replace("%5BREDACTED%5D", REDACTED)


def contains_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (is_secret_key(key) and item != REDACTED) or contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_secret(item) for item in value)
    if not isinstance(value, str):
        return False
    return bool(
        _HEADER_SECRET.search(value)
        or _BEARER.search(value)
        or _QR_DATA.search(value)
        or (_QUERY_SECRET.search(value) and "://" in value)
    )
