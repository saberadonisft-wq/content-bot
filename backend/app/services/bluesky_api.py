"""Typed public Bluesky AppView transport and post normalization helpers."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from ..crawlers.target_detection import InvalidTargetUrl, normalize_target_url

_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_PUBLIC_APPVIEW = "https://public.api.bsky.app"
_DIRECT_APPVIEW = "https://api.bsky.app"
_POST_URI = re.compile(
    r"^at://(?P<did>did:[a-z0-9]+:[A-Za-z0-9._:%-]+)/app\.bsky\.feed\.post/(?P<rkey>[A-Za-z0-9._:~-]{1,512})$"
)


async def bluesky_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    params: dict[str, Any],
    attempts: int = 3,
    before_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if not (
        endpoint.startswith("/xrpc/app.bsky.")
        or endpoint == "/xrpc/com.atproto.identity.resolveHandle"
    ):
        raise ValueError("Bluesky endpoint is outside the allowed public namespace")
    response: httpx.Response | Any | None = None
    for attempt in range(attempts):
        if before_request is not None:
            before_request()
        try:
            response = await client.get(endpoint, params=params)
        except httpx.TransportError as exc:
            if attempt + 1 >= attempts:
                raise CrawlerFailure(
                    CrawlerErrorCode.TRANSPORT_ERROR,
                    "Bluesky AppView request failed.",
                    retryable=True,
                ) from exc
            await asyncio.sleep(0.25 * (2**attempt))
            continue
        status = int(getattr(response, "status_code", 200))
        if status not in _RETRYABLE or attempt + 1 >= attempts:
            break
        await asyncio.sleep(0.25 * (2**attempt))

    assert response is not None
    status = int(getattr(response, "status_code", 200))
    # Bluesky documents both AppView hosts for direct public reads. Some edge
    # networks deny the cached public hostname while the direct AppView remains
    # available. Fail over only for that exact official base URL and only once;
    # never follow a provider-controlled alternate host.
    if status == 403 and _uses_public_appview(client):
        if before_request is not None:
            before_request()
        try:
            response = await client.get(
                f"{_DIRECT_APPVIEW}{endpoint}", params=params
            )
        except httpx.TransportError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "Bluesky direct AppView request failed.",
                retryable=True,
            ) from exc
        status = int(getattr(response, "status_code", 200))
    if status >= 400:
        raise _failure(response, status)
    try:
        payload = response.json()
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bluesky AppView returned invalid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bluesky AppView returned an invalid response.",
        )
    return payload


def _uses_public_appview(client: Any) -> bool:
    base_url = getattr(client, "base_url", None)
    if base_url is None:
        return False
    return str(base_url).rstrip("/") == _PUBLIC_APPVIEW


def is_invalid_cursor(failure: CrawlerFailure) -> bool:
    return (
        failure.code is CrawlerErrorCode.PARSE_CHANGED
        and failure.details.get("provider_reason") == "InvalidRequest"
    )


def post_identity(post: dict[str, Any]) -> tuple[str, str] | None:
    uri = str(post.get("uri") or "")
    match = _POST_URI.fullmatch(uri)
    if not match:
        return None
    return uri, match.group("rkey")


def stable_post_id(uri: str) -> str | None:
    """Return the canonical pseudonymous identity used by stored Bluesky posts."""

    if not uri or not _POST_URI.fullmatch(uri):
        return None
    return "bsky:" + hashlib.sha256(uri.encode("utf-8")).hexdigest()[:32]


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def extract_tags(record: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for facet in record.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        for feature in facet.get("features") or []:
            if not isinstance(feature, dict):
                continue
            raw_tag = str(feature.get("tag") or "").strip().removeprefix("#")
            if (
                feature.get("$type") == "app.bsky.richtext.facet#tag"
                and raw_tag
                and len(raw_tag) <= 100
            ):
                tag = f"#{raw_tag}"
                if tag not in tags:
                    tags.append(tag)
    return tags


def minimized_post_payload(
    post: dict[str, Any],
    *,
    tags: list[str],
    discovery: Mapping[str, Any],
) -> dict[str, Any]:
    record = post.get("record") or {}
    reply = record.get("reply") if isinstance(record, dict) else None
    parent_uri = str(((reply or {}).get("parent") or {}).get("uri") or "")
    root_uri = str(((reply or {}).get("root") or {}).get("uri") or "")
    quote_uri = _quoted_record_uri(post.get("embed"))
    return {
        "provider_id": "bluesky_appview",
        "cid": str(post.get("cid") or "")[:512],
        "text": str(record.get("text") or "")[:4_000],
        "created_at": str(record.get("createdAt") or "")[:100],
        "indexed_at": str(post.get("indexedAt") or "")[:100],
        "langs": [str(item)[:50] for item in (record.get("langs") or [])[:10]],
        "tags": list(tags[:50]),
        "like_count": int(post.get("likeCount", 0) or 0),
        "reply_count": int(post.get("replyCount", 0) or 0),
        "repost_count": int(post.get("repostCount", 0) or 0),
        "quote_count": int(post.get("quoteCount", 0) or 0),
        "is_reply": bool(parent_uri),
        "reply_parent_id": _stable_uri_id(parent_uri),
        "reply_root_id": _stable_uri_id(root_uri),
        "is_quote": bool(quote_uri),
        "quoted_post_id": _stable_uri_id(quote_uri),
        "media": extract_media(post.get("embed")),
        **discovery,
    }


def extract_media(embed: Any) -> list[dict[str, Any]]:
    if not isinstance(embed, dict):
        return []
    embed_type = str(embed.get("$type") or "")
    if embed_type.endswith("recordWithMedia#view"):
        return extract_media(embed.get("media"))
    result: list[dict[str, Any]] = []
    if embed_type.endswith("images#view"):
        for image in embed.get("images") or []:
            if not isinstance(image, dict):
                continue
            url = _safe_https_url(image.get("fullsize"))
            if url:
                result.append(
                    {
                        "kind": "image",
                        "url": url,
                        "thumbnail_url": _safe_https_url(image.get("thumb")),
                        "alt": str(image.get("alt") or "")[:1_000],
                        "aspect_ratio": _aspect_ratio(image.get("aspectRatio")),
                    }
                )
    elif embed_type.endswith("video#view"):
        url = _safe_https_url(embed.get("playlist"))
        if url:
            result.append(
                {
                    "kind": "video",
                    "url": url,
                    "thumbnail_url": _safe_https_url(embed.get("thumbnail")),
                    "alt": str(embed.get("alt") or "")[:1_000],
                    "aspect_ratio": _aspect_ratio(embed.get("aspectRatio")),
                }
            )
    elif embed_type.endswith("external#view"):
        external = embed.get("external") or {}
        if isinstance(external, dict):
            url = _safe_https_url(external.get("uri"))
            if url:
                result.append(
                    {
                        "kind": "external",
                        "url": url,
                        "thumbnail_url": _safe_https_url(external.get("thumb")),
                        "title": str(external.get("title") or "")[:300],
                        "description": str(external.get("description") or "")[:1_000],
                    }
                )
    return result[:10]


def _quoted_record_uri(embed: Any) -> str:
    if not isinstance(embed, dict):
        return ""
    embed_type = str(embed.get("$type") or "")
    if not embed_type.endswith(("record#view", "recordWithMedia#view")):
        return ""
    record = embed.get("record") or {}
    if isinstance(record, dict) and isinstance(record.get("record"), dict):
        record = record["record"]
    uri = str(record.get("uri") or "") if isinstance(record, dict) else ""
    return uri if _POST_URI.fullmatch(uri) else ""


def _stable_uri_id(uri: str) -> str | None:
    return stable_post_id(uri)


def _safe_https_url(value: Any) -> str | None:
    try:
        normalized, _host = normalize_target_url(str(value or ""))
    except InvalidTargetUrl:
        return None
    return normalized if normalized.startswith("https://") else None


def _aspect_ratio(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        width = int(value.get("width") or 0)
        height = int(value.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if not 1 <= width <= 100_000 or not 1 <= height <= 100_000:
        return None
    return {"width": width, "height": height}


def _failure(response: httpx.Response | Any, status: int) -> CrawlerFailure:
    reason = _safe_error(response)
    details = {"provider_reason": reason} if reason else {}
    if reason in {"HandleNotFound", "NotFound"}:
        return CrawlerFailure(
            CrawlerErrorCode.NOT_FOUND,
            "Bluesky actor or record was not found.",
            details=details,
        )
    if status == 429:
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "Bluesky AppView rate limit was reached.",
            retryable=True,
            details=details,
        )
    if status in {401, 403}:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "Bluesky AppView denied the public request.",
            details=details,
        )
    if status == 404:
        return CrawlerFailure(
            CrawlerErrorCode.NOT_FOUND,
            "Bluesky actor or record was not found.",
            details=details,
        )
    if status == 400:
        return CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bluesky rejected the request or checkpoint cursor.",
            details=details,
        )
    return CrawlerFailure(
        CrawlerErrorCode.TRANSPORT_ERROR,
        "Bluesky AppView is temporarily unavailable.",
        retryable=status >= 500,
        details=details,
    )


def _safe_error(response: httpx.Response | Any) -> str | None:
    try:
        payload = response.json()
    except (AttributeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    reason = str(payload.get("error") or "")
    return reason if reason and len(reason) <= 100 and reason.replace("_", "").isalnum() else None
