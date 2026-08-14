"""Permission-gated Instagram hashtag discovery through the official Graph API."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx

from ...runtime import (
    CancellationToken,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    RunContext,
)
from .targets import InstagramTargetKind, parse_instagram_target

_GRAPH_VERSION = re.compile(r"v[1-9][0-9]*\.[0-9]+")
_GRAPH_ID = re.compile(r"[1-9][0-9]{4,40}")
_CURSOR_VALUE = re.compile(r"[A-Za-z0-9_-]{1,1024}")


class InstagramHashtagBudget:
    """Persistent rolling unique-hashtag quota and ID cache.

    The ledger stores only SHA-256 term digests, timestamps, and public Graph
    IDs. It deliberately does not store the access token or raw search terms.
    """

    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _locks: ClassVar[dict[str, threading.Lock]] = {}

    def __init__(
        self,
        path: Path,
        instagram_user_id: str,
        *,
        max_unique: int = 30,
        max_cached_ids: int = 2_048,
        window: timedelta = timedelta(days=7),
        clock=None,
    ) -> None:
        if not _GRAPH_ID.fullmatch(instagram_user_id):
            raise ValueError("Instagram Professional user ID is invalid")
        if max_unique < 1 or max_cached_ids < max_unique or window.total_seconds() <= 0:
            raise ValueError("Instagram hashtag budget is invalid")
        self.path = Path(path).resolve()
        self.account_digest = hashlib.sha256(
            instagram_user_id.encode("ascii")
        ).hexdigest()
        self.max_unique = max_unique
        self.max_cached_ids = max_cached_ids
        self.window = window
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.path), threading.Lock())

    def reserve(self, hashtag: str) -> str | None:
        """Count a unique hashtag in the rolling window and return cached ID."""
        key = self._key(hashtag)
        now = self._now()
        with self._lock:
            state = self._load()
            cutoff = now - self.window
            state["uses"] = {
                digest: timestamp
                for digest, timestamp in state["uses"].items()
                if self._parse_time(timestamp) >= cutoff
            }
            if key not in state["uses"] and len(state["uses"]) >= self.max_unique:
                retry_at = min(
                    self._parse_time(timestamp)
                    for timestamp in state["uses"].values()
                ) + self.window
                raise CrawlerFailure(
                    CrawlerErrorCode.RATE_LIMITED,
                    "Instagram rolling unique-hashtag budget was reached.",
                    retryable=True,
                    retry_after_seconds=max(0, (retry_at - now).total_seconds()),
                )
            state["uses"][key] = now.isoformat()
            self._save(state)
            cached = state["hashtag_ids"].get(key)
            return str(cached) if cached else None

    def remember(self, hashtag: str, hashtag_id: str) -> None:
        if not _GRAPH_ID.fullmatch(hashtag_id):
            raise ValueError("Instagram hashtag identity is invalid")
        key = self._key(hashtag)
        with self._lock:
            state = self._load()
            if key not in state["hashtag_ids"]:
                while len(state["hashtag_ids"]) >= self.max_cached_ids:
                    state["hashtag_ids"].pop(next(iter(state["hashtag_ids"])))
            state["hashtag_ids"][key] = hashtag_id
            self._save(state)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise self._storage_failure() from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != 1
            or payload.get("account_digest") != self.account_digest
            or not isinstance(payload.get("uses"), Mapping)
            or not isinstance(payload.get("hashtag_ids"), Mapping)
        ):
            raise self._storage_failure()
        uses: dict[str, str] = {}
        hashtag_ids: dict[str, str] = {}
        try:
            for key, value in payload["uses"].items():
                if re.fullmatch(r"[0-9a-f]{64}", str(key)):
                    uses[str(key)] = self._parse_time(value).isoformat()
            for key, value in payload["hashtag_ids"].items():
                if re.fullmatch(r"[0-9a-f]{64}", str(key)) and _GRAPH_ID.fullmatch(
                    str(value)
                ):
                    hashtag_ids[str(key)] = str(value)
        except (TypeError, ValueError) as exc:
            raise self._storage_failure() from exc
        return {
            "schema_version": 1,
            "account_digest": self.account_digest,
            "uses": uses,
            "hashtag_ids": hashtag_ids,
        }

    def _save(self, state: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.tmp"
            )
            temporary.write_text(
                json.dumps(state, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(self.path)
        except OSError as exc:
            raise self._storage_failure() from exc

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "account_digest": self.account_digest,
            "uses": {},
            "hashtag_ids": {},
        }

    @staticmethod
    def _key(hashtag: str) -> str:
        normalized = _normalize_hashtag(hashtag).casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _now(self) -> datetime:
        value = self._clock()
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _storage_failure() -> CrawlerFailure:
        return CrawlerFailure(
            CrawlerErrorCode.STORAGE_ERROR,
            "Instagram hashtag budget ledger is unavailable or invalid.",
        )


@dataclass(frozen=True, slots=True)
class MetaGraphConfig:
    api_version: str
    access_token: str = field(repr=False)
    instagram_user_id: str
    base_url: str = "https://graph.facebook.com"

    def __post_init__(self) -> None:
        if not _GRAPH_VERSION.fullmatch(self.api_version):
            raise ValueError("Meta Graph API version must be explicitly pinned")
        if not self.access_token.strip() or len(self.access_token) > 4096:
            raise ValueError("Meta access token is missing or invalid")
        if not _GRAPH_ID.fullmatch(self.instagram_user_id):
            raise ValueError("Instagram Professional user ID is invalid")
        if self.base_url != "https://graph.facebook.com":
            raise ValueError("Meta Graph API host is not approved")


@dataclass(frozen=True, slots=True)
class InstagramMedia:
    media_id: str
    permalink: str
    caption: str = ""
    author_id: str = ""
    media_type: str = ""
    published_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InstagramMediaPage:
    items: tuple[InstagramMedia, ...]
    next_after: str | None


@dataclass(frozen=True, slots=True)
class InstagramHashtagCursor:
    term_index: int = 0
    hashtag_id: str = ""
    after: str = ""

    def __post_init__(self) -> None:
        if self.term_index < 0:
            raise ValueError("Instagram term index cannot be negative")
        if self.hashtag_id and not _GRAPH_ID.fullmatch(self.hashtag_id):
            raise ValueError("Instagram hashtag identity is invalid")
        if self.after and not _CURSOR_VALUE.fullmatch(self.after):
            raise ValueError("Instagram Graph cursor is invalid")

    def encode(self) -> str:
        payload = json.dumps(
            [self.term_index, self.hashtag_id, self.after],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return "v1i:" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> InstagramHashtagCursor:
        if value is None:
            return cls()
        if not value.startswith("v1i:") or len(value) > 2_000:
            raise _parse_failure("Instagram hashtag checkpoint is invalid.")
        encoded = value[4:]
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(encoded + padding)
            decoded = json.loads(raw)
            if not isinstance(decoded, list) or len(decoded) != 3:
                raise ValueError
            return cls(int(decoded[0]), str(decoded[1]), str(decoded[2]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise _parse_failure("Instagram hashtag checkpoint is invalid.") from exc


class InstagramHashtagProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def resolve_hashtag(self, hashtag: str) -> str: ...

    async def recent_media(
        self, hashtag_id: str, *, after: str | None, limit: int
    ) -> InstagramMediaPage: ...

    async def close(self) -> None: ...


class InstagramGraphHashtagProvider:
    """Small official transport; no browser/cookie fallback is permitted."""

    def __init__(
        self,
        config: MetaGraphConfig,
        client: httpx.AsyncClient | None = None,
        budget: InstagramHashtagBudget | None = None,
    ) -> None:
        self.config = config
        if client is not None:
            expected = f"{config.base_url}/{config.api_version}"
            if str(client.base_url).rstrip("/") != expected:
                raise ValueError("Injected Meta Graph client has an unapproved base URL")
            if client.follow_redirects:
                raise ValueError("Meta Graph client must not follow redirects")
        self._client = client
        self._owns_client = client is None
        self._cancellation: CancellationToken | None = None
        self._budget = budget

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != "instagram" or context.operation != "search":
            raise ValueError("Instagram hashtag provider context is invalid")
        self._cancellation = cancellation
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.config.base_url}/{self.config.api_version}",
                headers={"Authorization": f"Bearer {self.config.access_token}"},
                timeout=30,
                follow_redirects=False,
            )

    async def resolve_hashtag(self, hashtag: str) -> str:
        normalized = _normalize_hashtag(hashtag)
        cached = self._budget.reserve(normalized) if self._budget else None
        if cached:
            return cached
        payload = await self._get(
            "/ig_hashtag_search",
            params={
                "user_id": self.config.instagram_user_id,
                "q": normalized,
            },
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise CrawlerFailure(
                CrawlerErrorCode.NOT_FOUND,
                "Instagram hashtag was not found for the authorized account.",
            )
        hashtag_id = str((rows[0] or {}).get("id") or "")
        if not _GRAPH_ID.fullmatch(hashtag_id):
            raise _parse_failure("Instagram hashtag lookup response changed.")
        if self._budget:
            self._budget.remember(normalized, hashtag_id)
        return hashtag_id

    async def recent_media(
        self, hashtag_id: str, *, after: str | None, limit: int
    ) -> InstagramMediaPage:
        if not _GRAPH_ID.fullmatch(hashtag_id) or not 1 <= limit <= 100:
            raise ValueError("Instagram hashtag page request is invalid")
        params: dict[str, str | int] = {
            "user_id": self.config.instagram_user_id,
            "fields": "id,caption,media_type,permalink,timestamp,username,like_count,comments_count",
            "limit": limit,
        }
        if after:
            if not _CURSOR_VALUE.fullmatch(after):
                raise ValueError("Instagram Graph cursor is invalid")
            params["after"] = after
        payload = await self._get(f"/{hashtag_id}/recent_media", params=params)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise _parse_failure("Instagram recent-media response changed.")
        items = tuple(
            media for row in rows if (media := _parse_media(row)) is not None
        )
        paging = payload.get("paging")
        cursors = paging.get("cursors") if isinstance(paging, Mapping) else None
        next_after = (
            str(cursors.get("after") or "")
            if isinstance(cursors, Mapping) and paging.get("next")
            else ""
        )
        if next_after and not _CURSOR_VALUE.fullmatch(next_after):
            raise _parse_failure("Instagram pagination cursor changed.")
        return InstagramMediaPage(items, next_after or None)

    async def close(self) -> None:
        client, self._client = self._client, None
        self._cancellation = None
        if client is not None and self._owns_client:
            await client.aclose()

    async def _get(
        self, path: str, *, params: Mapping[str, str | int]
    ) -> Mapping[str, Any]:
        if self._client is None or self._cancellation is None:
            raise RuntimeError("Instagram Graph provider is not open")
        self._cancellation.raise_if_cancelled()
        try:
            response = await self._client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {self.config.access_token}"},
            )
        except httpx.HTTPError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "Instagram Graph API request failed.",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise _response_failure(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _parse_failure("Instagram Graph API returned invalid JSON.") from exc
        if not isinstance(payload, Mapping):
            raise _parse_failure("Instagram Graph API returned an invalid response.")
        return payload


class InstagramHashtagSearchAdapter:
    source_id = "instagram"
    provider_id = "instagram_hashtag"
    owns_resources = True

    def __init__(
        self,
        provider: InstagramHashtagProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if not context.terms:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Instagram hashtag search requires at least one hashtag.",
            )
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[ContentRecord, str]:
        if self._cancellation is None:
            raise RuntimeError("Instagram hashtag adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise _parse_failure("Instagram hashtag checkpoint shape is invalid.")
        state = InstagramHashtagCursor.decode(cursor)
        if state.term_index >= len(context.terms):
            return Page((), None, False)
        self._cancellation.raise_if_cancelled()
        hashtag_id = state.hashtag_id or await self.provider.resolve_hashtag(
            context.terms[state.term_index]
        )
        page = await self.provider.recent_media(
            hashtag_id,
            after=state.after or None,
            limit=min(limit, 100),
        )
        records = tuple(
            normalize_instagram_media(item, self.pseudonymizer)
            for item in page.items[:limit]
        )
        if page.next_after:
            next_state = InstagramHashtagCursor(
                state.term_index, hashtag_id, page.next_after
            )
        elif state.term_index + 1 < len(context.terms):
            next_state = InstagramHashtagCursor(state.term_index + 1)
        else:
            next_state = None
        return Page(
            records,
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._cancellation = None
        await self.provider.close()


def normalize_instagram_media(
    media: InstagramMedia,
    pseudonymizer: IdentityPseudonymizer,
) -> ContentRecord:
    try:
        target = parse_instagram_target(media.permalink)
    except ValueError as exc:
        raise _parse_failure("Instagram media permalink is invalid.") from exc
    if target.kind is not InstagramTargetKind.MEDIA or not _GRAPH_ID.fullmatch(
        media.media_id
    ):
        raise _parse_failure("Instagram media identity is invalid.")
    caption = media.caption.strip()
    title = next(
        (line.strip() for line in caption.splitlines() if line.strip()),
        f"Instagram media {target.external_id}",
    )[:180]
    published_at = media.published_at
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    metrics = {
        key: value
        for key, value in media.metrics.items()
        if key in {"like_count", "comment_count"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return ContentRecord(
        source_id="instagram",
        external_id=media.media_id,
        canonical_url=target.canonical_url,
        title=title,
        body=caption[:4_000],
        author_pseudonym=pseudonymizer.pseudonym(
            "instagram", media.author_id
        ),
        published_at=published_at,
        metrics=metrics,
        provenance={
            "provider_id": "instagram_hashtag",
            "contract_version": "cbce.instagram.hashtag-media.v1",
            "coverage": "best_effort",
            "media_type": media.media_type[:40],
        },
    )


def _parse_media(value: Any) -> InstagramMedia | None:
    if not isinstance(value, Mapping):
        return None
    media_id = str(value.get("id") or "")
    permalink = str(value.get("permalink") or "")
    if not _GRAPH_ID.fullmatch(media_id) or not permalink:
        return None
    timestamp = value.get("timestamp")
    published_at = None
    if timestamp:
        try:
            published_at = datetime.fromisoformat(str(timestamp))
        except ValueError:
            published_at = None
    return InstagramMedia(
        media_id=media_id,
        permalink=permalink,
        caption=str(value.get("caption") or "")[:10_000],
        author_id=str(value.get("username") or "")[:200],
        media_type=str(value.get("media_type") or "")[:40],
        published_at=published_at,
        metrics={
            "like_count": _safe_count(value.get("like_count")),
            "comment_count": _safe_count(value.get("comments_count")),
        },
    )


def _normalize_hashtag(value: str) -> str:
    normalized = str(value).strip().removeprefix("#")
    if not re.fullmatch(r"[^\s#]{1,100}", normalized):
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Instagram hashtag is invalid.",
        )
    return normalized


def _safe_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _response_failure(response: httpx.Response) -> CrawlerFailure:
    status = response.status_code
    error_code = None
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, Mapping) else None
        if isinstance(error, Mapping):
            error_code = int(error.get("code")) if error.get("code") is not None else None
    except (ValueError, TypeError):
        pass
    if status == 401 or error_code == 190:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "Instagram Graph access token is invalid or expired.",
        )
    if status == 403 or error_code in {10, 200}:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "Instagram Graph permission or App Review access is required.",
        )
    if status == 429 or error_code in {4, 17, 32, 613}:
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "Instagram Graph API rate limit was reached.",
            retryable=True,
        )
    if status == 404:
        return CrawlerFailure(
            CrawlerErrorCode.NOT_FOUND,
            "Instagram Graph resource was not found.",
        )
    if status >= 500:
        return CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "Instagram Graph API is temporarily unavailable.",
            retryable=True,
        )
    return CrawlerFailure(
        CrawlerErrorCode.PERMISSION_REQUIRED,
        "Instagram Graph request was rejected.",
    )


def _parse_failure(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)
