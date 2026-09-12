from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import http_pool


@asynccontextmanager
async def pooled_client(**kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Borrow a client from the shared pool; lifespan owns its shutdown."""
    client = await http_pool.get_client(**kwargs)
    yield client


DEFAULT_WEB_FEED_URLS = (
    "https://news.google.com/rss/search?q={query}&hl=vi&gl=VN&ceid=VN:vi"
)


RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


logger = logging.getLogger(__name__)


def stable_external_id(namespace: str, public_identity: str) -> str:
    """Create a stable pseudonymous ID without persisting provider identity blobs."""
    digest = hashlib.sha256(public_identity.strip().encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{digest}"


def plain_text(value: object, limit: int = 4_000) -> str:
    """Convert small provider HTML fragments into bounded readable text."""
    import re

    text = re.sub(r"<br\s*/?>|</p>", "\n", str(value or ""), flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()[:limit]


def parse_feed_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def allocate_limits(total: int, buckets: int) -> list[int]:
    if buckets <= 0:
        return []
    base, remainder = divmod(total, buckets)
    return [base + (1 if index < remainder else 0) for index in range(buckets)]


async def get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> httpx.Response:
    """GET with bounded retries for transient transport and HTTP failures."""
    for attempt in range(attempts):
        try:
            response = (
                await client.get(url, params=params)
                if params is not None
                else await client.get(url)
            )
            status_code = int(getattr(response, "status_code", 200))
            if status_code not in RETRYABLE_HTTP_STATUSES or attempt == attempts - 1:
                response.raise_for_status()
                return response
            retry_after = getattr(response, "headers", {}).get("Retry-After")
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            retry_after = None
        try:
            delay = (
                min(max(float(retry_after), 0), 5)
                if retry_after
                else 0.5 * (2**attempt)
            )
        except ValueError:
            delay = 0.5 * (2**attempt)
        await asyncio.sleep(delay)
    raise RuntimeError("HTTP retry loop ended unexpectedly")


def login_progress_from_stderr(line: str) -> tuple[str, str] | None:
    """Recognize login confirmation without exposing cookies or QR payloads."""
    stripped = line.strip()
    if stripped.startswith("CONTENT_BOT_PROGRESS "):
        try:
            payload = json.loads(stripped.removeprefix("CONTENT_BOT_PROGRESS "))
        except json.JSONDecodeError:
            return None
        status = payload.get("status")
        if status in {"authenticated", "retrying"}:
            return str(status), str(payload.get("message") or "Login confirmed")
        return None

    lowered = stripped.casefold()
    markers = (
        "login successful",
        "login status confirmed",
        "login state result: true",
        "use cache login state",
        "login state verified",
        "ping zhihu successfully",
    )
    if any(marker in lowered for marker in markers):
        return "authenticated", "Login confirmed by MediaCrawler"
    return None
