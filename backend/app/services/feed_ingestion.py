"""Validated RSS/Atom template and HTTP transport primitives."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from dataclasses import dataclass
from string import Formatter
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx

from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from ..crawlers.target_detection import InvalidTargetUrl, normalize_target_url

MAX_FEED_BYTES = 2_000_000
MAX_REDIRECTS = 3
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_XML_MEDIA_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/rss+xml",
        "application/xml",
        "text/xml",
    }
)


@dataclass(frozen=True, slots=True)
class FeedTemplate:
    value: str
    host: str
    digest: str

    def render(self, search_term: str) -> str:
        return self.value.format(query=quote_plus(search_term))


@dataclass(frozen=True, slots=True)
class FeedDocument:
    url: str
    content: bytes
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


def parse_feed_templates(value: str) -> tuple[FeedTemplate, ...]:
    """Parse legacy comma input plus newline/JSON formats that preserve commas."""
    text = str(value or "").strip()
    if not text:
        return ()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Feed template JSON is invalid") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("Feed template JSON must be an array of URLs")
        values = [item.strip() for item in parsed if item.strip()]
    elif "\n" in text or "\r" in text:
        values = [item.strip() for item in text.splitlines() if item.strip()]
    else:
        values = [item.strip() for item in text.split(",") if item.strip()]

    result: list[FeedTemplate] = []
    seen: set[str] = set()
    for template in values:
        fields = []
        try:
            for _literal, field_name, format_spec, conversion in Formatter().parse(template):
                if field_name is None:
                    continue
                if field_name != "query" or format_spec or conversion:
                    raise ValueError("Feed templates may contain only the {query} placeholder")
                fields.append(field_name)
        except ValueError as exc:
            raise ValueError("Feed template contains invalid braces or placeholders") from exc
        if len(fields) > 1:
            raise ValueError("Feed template may contain {query} at most once")
        rendered = template.format(query="content-bot-feed-check")
        try:
            normalized, host = normalize_target_url(rendered)
        except InvalidTargetUrl as exc:
            raise ValueError("Feed template must be an absolute HTTPS URL") from exc
        if not normalized.startswith("https://"):
            raise ValueError("Feed template must use HTTPS")
        _reject_local_host(host)
        digest = hashlib.sha256(template.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        result.append(FeedTemplate(template, host, digest))
    return tuple(result)


async def fetch_feed_document(
    client: httpx.AsyncClient,
    url: str,
    *,
    allowed_hosts: frozenset[str],
    validators: dict[str, Any] | None = None,
    attempts: int = 3,
) -> FeedDocument:
    """Fetch a bounded feed, allowing redirects only to configured feed hosts."""
    if not 1 <= attempts <= 5:
        raise ValueError("Feed request attempts must be between 1 and 5")
    headers: dict[str, str] = {}
    etag = str((validators or {}).get("etag") or "").strip()
    last_modified = str((validators or {}).get("last_modified") or "").strip()
    if etag:
        headers["If-None-Match"] = etag[:500]
    if last_modified:
        headers["If-Modified-Since"] = last_modified[:500]

    current_url = _validated_feed_url(url, allowed_hosts)
    redirects = 0
    response: httpx.Response | Any | None = None
    attempt = 0
    while attempt < attempts:
        try:
            response = (
                await client.get(current_url, headers=headers)
                if headers
                else await client.get(current_url)
            )
        except httpx.TransportError as exc:
            attempt += 1
            if attempt >= attempts:
                raise CrawlerFailure(
                    CrawlerErrorCode.TRANSPORT_ERROR,
                    "Feed request failed.",
                    retryable=True,
                ) from exc
            await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
            continue

        status = int(getattr(response, "status_code", 200))
        if status in {301, 302, 303, 307, 308}:
            location = str(getattr(response, "headers", {}).get("Location") or "")
            redirects += 1
            if not location or redirects > MAX_REDIRECTS:
                raise CrawlerFailure(
                    CrawlerErrorCode.TRANSPORT_ERROR,
                    "Feed redirect policy was exceeded.",
                )
            current_url = _validated_feed_url(
                urljoin(current_url, location),
                allowed_hosts,
            )
            continue
        if status not in _RETRYABLE:
            break
        attempt += 1
        if attempt >= attempts:
            break
        await asyncio.sleep(0.25 * (2 ** (attempt - 1)))

    assert response is not None
    status = int(getattr(response, "status_code", 200))
    if status == 304:
        return FeedDocument(current_url, b"", etag or None, last_modified or None, True)
    if status == 429:
        raise CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "Feed host rate limit was reached.",
            retryable=True,
        )
    if status >= 400:
        raise CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "Feed host returned an HTTP error.",
            retryable=status >= 500,
        )

    content = bytes(getattr(response, "content", b""))
    if not content or len(content) > MAX_FEED_BYTES:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Feed document is empty or exceeds the size limit.",
        )
    media_type = str(getattr(response, "headers", {}).get("Content-Type") or "")
    media_type = media_type.split(";", 1)[0].strip().casefold()
    if media_type and media_type not in _XML_MEDIA_TYPES:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Feed response is not RSS/Atom XML.",
        )
    if not _looks_like_feed_xml(content):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Feed response does not identify an RSS or Atom document.",
        )
    response_headers = getattr(response, "headers", {})
    return FeedDocument(
        current_url,
        content,
        str(response_headers.get("ETag") or "").strip()[:500] or None,
        str(response_headers.get("Last-Modified") or "").strip()[:500] or None,
    )


def safe_entry_url(value: str, *, base_url: str) -> str | None:
    candidate = urljoin(base_url, str(value or "").strip())
    try:
        normalized, host = normalize_target_url(candidate)
        _reject_local_host(host)
    except (InvalidTargetUrl, ValueError):
        return None
    return normalized if normalized.startswith("https://") else None


def _validated_feed_url(url: str, allowed_hosts: frozenset[str]) -> str:
    try:
        normalized, host = normalize_target_url(url)
    except InvalidTargetUrl as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Feed URL is invalid.",
        ) from exc
    if not normalized.startswith("https://") or host not in allowed_hosts:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Feed URL or redirect host is not allowlisted.",
        )
    try:
        _reject_local_host(host)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Feed URL must identify a public host.",
        ) from exc
    return normalized


def _reject_local_host(host: str) -> None:
    lowered = host.casefold().rstrip(".")
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Feed host must be public")
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Feed host must be public")


def _looks_like_feed_xml(content: bytes) -> bool:
    prefix = content.lstrip()[:1_024].lower()
    if prefix.startswith(b"<?xml"):
        closing = prefix.find(b"?>")
        prefix = prefix[closing + 2 :].lstrip() if closing >= 0 else prefix
    return prefix.startswith((b"<rss", b"<feed", b"<rdf:rdf"))
