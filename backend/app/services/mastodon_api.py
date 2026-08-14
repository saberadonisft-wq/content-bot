"""Public Mastodon instance transport, pagination and status normalization."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from ..crawlers.target_detection import (
    InvalidTargetUrl,
    normalize_host,
    normalize_target_url,
)

DEFAULT_MASTODON_INSTANCES = (
    "mastodon.social",
    "mastodon.gamedev.place",
    "dice.camp",
)
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_LINK = re.compile(r"<([^>]+)>\s*;\s*rel=\"?([^\";,]+)\"?", re.IGNORECASE)


@dataclass
class MastodonRequestBudget:
    remaining: int

    def consume(self) -> None:
        if self.remaining <= 0:
            raise CrawlerFailure(
                CrawlerErrorCode.BUDGET_EXHAUSTED,
                "Mastodon request budget was exhausted.",
            )
        self.remaining -= 1


class _FragmentTextParser(HTMLParser):
    _BLOCKS = frozenset({"br", "p", "div", "li", "blockquote", "pre"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def mastodon_instances(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    if raw is None or raw == "":
        values: Iterable[Any] = DEFAULT_MASTODON_INSTANCES
    elif isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("MASTODON_INSTANCES must be a JSON array or delimited host list") from exc
            if not isinstance(parsed, list):
                raise ValueError("MASTODON_INSTANCES JSON must be an array")
            values = parsed
        else:
            values = re.split(r"[,\r\n]+", text)
    else:
        values = raw

    result: list[str] = []
    for value in values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        try:
            if "://" in candidate:
                normalized, host = normalize_target_url(candidate)
                parsed = urlsplit(normalized)
                if parsed.scheme != "https" or parsed.path not in {"", "/"} or parsed.query:
                    raise ValueError("Mastodon instances must be HTTPS origins")
            else:
                host = normalize_host(candidate)
        except InvalidTargetUrl as exc:
            raise ValueError("Mastodon instance host is invalid") from exc
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Mastodon instance must not be a private or local address")
        if host not in result:
            result.append(host)
    if not result:
        raise ValueError("At least one Mastodon instance is required")
    return tuple(result)


def normalize_hashtag(value: str) -> str:
    return "".join(char for char in value.casefold().removeprefix("#") if char.isalnum())[:100]


async def mastodon_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    budget: MastodonRequestBudget | None = None,
    attempts: int = 3,
    before_request: Callable[[], None] | None = None,
) -> tuple[Any, httpx.Response | Any]:
    if not endpoint.startswith("/api/"):
        raise ValueError("Mastodon endpoint is outside the allowed API namespace")
    if budget is not None:
        budget.consume()
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
                    "Mastodon instance request failed.",
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
    if status >= 400:
        raise _failure(status)
    try:
        return response.json(), response
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Mastodon instance returned invalid JSON.",
        ) from exc


def next_max_id(response: httpx.Response | Any, posts: list[Any]) -> str | None:
    del posts
    headers = getattr(response, "headers", {}) or {}
    link_value = str(headers.get("Link") or headers.get("link") or "")
    for url, relation in _LINK.findall(link_value):
        if relation.casefold() != "next":
            continue
        values = parse_qs(urlsplit(url).query).get("max_id") or []
        if values and values[0]:
            return str(values[0])[:512]
    return None


def normalize_status(
    status: dict[str, Any],
    *,
    fetching_instance: str,
    discovery: dict[str, Any],
) -> dict[str, Any] | None:
    boosted = status.get("reblog")
    is_reblog = isinstance(boosted, dict)
    original = boosted if is_reblog else status
    assert isinstance(original, dict)
    identity = _safe_public_url(original.get("uri")) or _safe_public_url(original.get("url"))
    canonical_url = _safe_public_url(original.get("url")) or identity
    if not identity or not canonical_url:
        return None
    body = html_text(original.get("content"))[:4_000]
    account = original.get("account") or {}
    author = str(account.get("acct") or account.get("username") or "")[:320]
    tags = []
    for item in original.get("tags") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().removeprefix("#")[:100]
        if name and f"#{name}" not in tags:
            tags.append(f"#{name}")
    local_reply_id = str(original.get("in_reply_to_id") or "")[:512]
    return {
        "identity": identity,
        "external_id": mastodon_external_id(identity),
        "canonical_url": canonical_url,
        "title": next((line.strip() for line in body.splitlines() if line.strip()), body)[:180],
        "body": body,
        "author": author,
        "hashtags": tags[:50],
        "locale": str(original.get("language") or "")[:50] or None,
        "published_at": parse_datetime(original.get("created_at")),
        "metrics": {
            "like_count": _count(original.get("favourites_count")),
            "comment_count": _count(original.get("replies_count")),
            "share_count": _count(original.get("reblogs_count")),
        },
        "raw_payload": {
            "provider_id": "mastodon_public",
            "content_version": str(original.get("edited_at") or original.get("created_at") or "")[:100],
            "fetching_instance": fetching_instance,
            "is_reblog": is_reblog,
            "is_reply": bool(local_reply_id),
            "reply_local_id": (
                mastodon_external_id(f"https://{fetching_instance}/api/status/{local_reply_id}")
                if local_reply_id
                else None
            ),
            "sensitive": bool(original.get("sensitive")),
            "spoiler_text": html_text(original.get("spoiler_text"))[:500],
            "media": _media(original.get("media_attachments")),
            "favorite_count": _count(original.get("favourites_count")),
            "reply_count": _count(original.get("replies_count")),
            "reblog_count": _count(original.get("reblogs_count")),
            **discovery,
        },
    }


def mastodon_external_id(identity: str) -> str:
    digest = hashlib.sha256(identity.strip().encode("utf-8")).hexdigest()[:32]
    return f"mastodon:{digest}"


def html_text(value: Any) -> str:
    parser = _FragmentTextParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:
        return ""
    text = "".join(parser.parts).replace("\r", "")
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _failure(status: int) -> CrawlerFailure:
    if status == 401:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "This Mastodon instance requires an app token for public timelines.",
        )
    if status == 403:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "This Mastodon instance disabled the requested public surface.",
        )
    if status == 404:
        return CrawlerFailure(CrawlerErrorCode.NOT_FOUND, "Mastodon target was not found.")
    if status == 429:
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "Mastodon instance rate limit was reached.",
            retryable=True,
        )
    return CrawlerFailure(
        CrawlerErrorCode.TRANSPORT_ERROR,
        "Mastodon instance is temporarily unavailable.",
        retryable=status >= 500,
    )


def _safe_public_url(value: Any) -> str | None:
    try:
        normalized, _host = normalize_target_url(str(value or ""))
    except InvalidTargetUrl:
        return None
    return normalized if normalized.startswith("https://") else None


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _media(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        url = _safe_public_url(item.get("url"))
        if not url:
            continue
        result.append(
            {
                "kind": str(item.get("type") or "unknown")[:30],
                "url": url,
                "preview_url": _safe_public_url(item.get("preview_url")),
                "description": str(item.get("description") or "")[:1_000],
            }
        )
    return result[:10]
