"""Official X API v2 read-only provider.

The provider never posts, likes, follows or falls back to browser scraping.
Endpoint and field choices are sourced from docs.x.com and recorded in the X
provenance ledger.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ...runtime import (
    CancellationToken,
    CommentRecord,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    RunContext,
)
from .targets import XTargetKind, parse_x_target

_POST_FIELDS = (
    "id,text,author_id,created_at,lang,public_metrics,conversation_id,"
    "referenced_tweets,attachments,entities"
)
_MEDIA_FIELDS = "type,url,preview_image_url,width,height,duration_ms"
_EXPANSIONS = "attachments.media_keys"
_POST_ID = re.compile(r"[1-9][0-9]{0,18}")
_USERNAME = re.compile(r"[A-Za-z0-9_]{1,15}")


@dataclass(frozen=True, slots=True)
class XPost:
    post_id: str
    text: str
    author_id: str = ""
    created_at: datetime | None = None
    language: str | None = None
    conversation_id: str | None = None
    parent_id: str | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)
    hashtags: tuple[str, ...] = ()
    media: tuple[Mapping[str, object], ...] = ()

    @property
    def canonical_url(self) -> str:
        return f"https://x.com/i/web/status/{self.post_id}"


@dataclass(frozen=True, slots=True)
class XPostPage:
    items: tuple[XPost, ...]
    next_token: str | None


class XReadProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def recent_search(
        self,
        query: str,
        *,
        max_results: int,
        next_token: str | None = None,
        until_id: str | None = None,
    ) -> XPostPage: ...

    async def fetch_post(self, post_id: str) -> XPost: ...

    async def creator_posts(
        self,
        username: str,
        *,
        max_results: int,
        pagination_token: str | None = None,
        until_id: str | None = None,
        exclude_replies: bool = False,
        exclude_reposts: bool = False,
    ) -> XPostPage: ...

    async def conversation_replies(
        self,
        post_id: str,
        *,
        max_results: int,
        next_token: str | None = None,
        until_id: str | None = None,
    ) -> XPostPage: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class XSearchCursor:
    term_index: int = 0
    next_token: str | None = None
    until_id: str | None = None

    def __post_init__(self) -> None:
        if self.term_index < 0 or (self.next_token and self.until_id):
            raise ValueError("Invalid X search cursor")
        if self.until_id is not None and not _POST_ID.fullmatch(self.until_id):
            raise ValueError("Invalid X until_id cursor")
        if self.next_token is not None and not 1 <= len(self.next_token) <= 1_024:
            raise ValueError("Invalid X pagination token")

    def encode(self) -> str:
        raw = json.dumps(
            {
                "t": self.term_index,
                "n": self.next_token,
                "u": self.until_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return "v1x:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> XSearchCursor:
        if value is None:
            return cls()
        if not value.startswith("v1x:") or len(value) > 2_048:
            raise _parse_failure("X checkpoint cursor is invalid.")
        encoded = value[4:]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"n", "t", "u"}:
                raise ValueError
            return cls(
                int(payload["t"]),
                str(payload["n"]) if payload["n"] is not None else None,
                str(payload["u"]) if payload["u"] is not None else None,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _parse_failure("X checkpoint cursor is invalid.") from exc


@dataclass(frozen=True, slots=True)
class XTargetCursor:
    next_token: str | None = None
    until_id: str | None = None

    def __post_init__(self) -> None:
        if self.next_token and self.until_id:
            raise ValueError("Invalid X target cursor")
        if self.until_id is not None and not _POST_ID.fullmatch(self.until_id):
            raise ValueError("Invalid X until_id cursor")
        if self.next_token is not None and not 1 <= len(self.next_token) <= 1_024:
            raise ValueError("Invalid X pagination token")

    def encode(self) -> str:
        raw = json.dumps(
            {"n": self.next_token, "u": self.until_id},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return "v1t:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> XTargetCursor:
        if value is None:
            return cls()
        if not value.startswith("v1t:") or len(value) > 2_048:
            raise _parse_failure("X target checkpoint cursor is invalid.")
        encoded = value[4:]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"n", "u"}:
                raise ValueError
            return cls(
                str(payload["n"]) if payload["n"] is not None else None,
                str(payload["u"]) if payload["u"] is not None else None,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _parse_failure("X target checkpoint cursor is invalid.") from exc


class XApiClient:
    """Small official read client with safe, typed provider errors."""

    def __init__(
        self,
        bearer_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = "https://api.x.com",
    ) -> None:
        token = bearer_token.strip()
        if not token:
            raise ValueError("X bearer token is required")
        self._token = token
        self._transport = transport
        self._base_url = base_url
        self._client: httpx.AsyncClient | None = None

    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None:
        cancellation.raise_if_cancelled()
        if self._client is not None:
            raise RuntimeError("X API client is already open")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "ContentBot/0.1",
            },
            timeout=30,
            transport=self._transport,
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def recent_search(
        self,
        query: str,
        *,
        max_results: int,
        next_token: str | None = None,
        until_id: str | None = None,
    ) -> XPostPage:
        params: dict[str, object] = {
            "query": query,
            "max_results": max(10, min(100, max_results)),
            "tweet.fields": _POST_FIELDS,
            "expansions": _EXPANSIONS,
            "media.fields": _MEDIA_FIELDS,
        }
        if next_token:
            params["next_token"] = next_token
        if until_id:
            params["until_id"] = until_id
        payload = await self._get("/2/tweets/search/recent", params=params)
        return _parse_post_page(payload)

    async def fetch_post(self, post_id: str) -> XPost:
        if not _POST_ID.fullmatch(post_id):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X post identity is invalid."
            )
        payload = await self._get(
            f"/2/tweets/{post_id}",
            params={
                "tweet.fields": _POST_FIELDS,
                "expansions": _EXPANSIONS,
                "media.fields": _MEDIA_FIELDS,
            },
        )
        page = _parse_post_page(payload)
        if not page.items:
            raise CrawlerFailure(CrawlerErrorCode.NOT_FOUND, "X post was not found.")
        return page.items[0]

    async def creator_posts(
        self,
        username: str,
        *,
        max_results: int,
        pagination_token: str | None = None,
        until_id: str | None = None,
        exclude_replies: bool = False,
        exclude_reposts: bool = False,
    ) -> XPostPage:
        if not _USERNAME.fullmatch(username):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X creator username is invalid."
            )
        user_payload = await self._get(f"/2/users/by/username/{username}")
        user_data = user_payload.get("data")
        user_id = str(user_data.get("id") or "") if isinstance(user_data, dict) else ""
        if not _POST_ID.fullmatch(user_id):
            raise CrawlerFailure(CrawlerErrorCode.NOT_FOUND, "X creator was not found.")
        params: dict[str, object] = {
            "max_results": max(5, min(100, max_results)),
            "tweet.fields": _POST_FIELDS,
            "expansions": _EXPANSIONS,
            "media.fields": _MEDIA_FIELDS,
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        if until_id:
            params["until_id"] = until_id
        excluded: list[str] = []
        if exclude_replies:
            excluded.append("replies")
        if exclude_reposts:
            excluded.append("retweets")
        if excluded:
            params["exclude"] = ",".join(excluded)
        return _parse_post_page(await self._get(f"/2/users/{user_id}/tweets", params=params))

    async def conversation_replies(
        self,
        post_id: str,
        *,
        max_results: int,
        next_token: str | None = None,
        until_id: str | None = None,
    ) -> XPostPage:
        if not _POST_ID.fullmatch(post_id):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X conversation identity is invalid."
            )
        return await self.recent_search(
            f"conversation_id:{post_id} is:reply",
            max_results=max_results,
            next_token=next_token,
            until_id=until_id,
        )

    async def _get(
        self, path: str, *, params: Mapping[str, object] | None = None
    ) -> Mapping[str, Any]:
        if self._client is None:
            raise RuntimeError("X API client is not open")
        try:
            response = await self._client.get(path, params=params)
        except httpx.TransportError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "X API request failed.",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise _response_failure(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _parse_failure("X API returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise _parse_failure("X API returned an invalid response.")
        return payload


class XSearchAdapter:
    source_id = "x"
    provider_id = "x_api"
    owns_resources = True

    def __init__(
        self,
        provider: XReadProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if not context.terms:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X search requires at least one term."
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
            raise RuntimeError("X adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise _parse_failure("X checkpoint has an unsupported shape.")
        state = XSearchCursor.decode(cursor)
        if state.term_index >= len(context.terms):
            return Page((), None, False)
        self._cancellation.raise_if_cancelled()
        page = await self.provider.recent_search(
            context.terms[state.term_index],
            max_results=limit,
            next_token=state.next_token,
            until_id=state.until_id,
        )
        selected = page.items[:limit]
        records = tuple(
            normalize_x_post(post, self.pseudonymizer) for post in selected
        )
        if len(page.items) > len(selected) and selected:
            last_id = int(selected[-1].post_id)
            next_state = XSearchCursor(
                state.term_index,
                until_id=str(last_id - 1),
            )
        elif page.next_token:
            next_state = XSearchCursor(
                state.term_index,
                next_token=page.next_token,
            )
        elif state.term_index + 1 < len(context.terms):
            next_state = XSearchCursor(state.term_index + 1)
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


class XDetailAdapter:
    source_id = "x"
    provider_id = "x_api"
    owns_resources = True

    def __init__(
        self,
        provider: XReadProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._post_id: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url") or context.target.get("id")
        try:
            parsed = parse_x_target(str(target or ""))
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X detail target is invalid."
            ) from exc
        if parsed.kind is not XTargetKind.POST:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X detail target is not a post."
            )
        self._post_id = parsed.external_id
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch(self) -> ContentRecord:
        if self._post_id is None or self._cancellation is None:
            raise RuntimeError("X detail adapter is not open")
        self._cancellation.raise_if_cancelled()
        return normalize_x_post(
            await self.provider.fetch_post(self._post_id), self.pseudonymizer
        )

    async def close(self) -> None:
        self._post_id = None
        self._cancellation = None
        await self.provider.close()


class XCreatorAdapter:
    source_id = "x"
    provider_id = "x_api"
    owns_resources = True

    def __init__(
        self,
        provider: XReadProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._username: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url") or context.target.get("username")
        try:
            parsed = parse_x_target(str(target or ""))
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X creator target is invalid."
            ) from exc
        if parsed.kind is not XTargetKind.CREATOR:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X creator target is not an account."
            )
        self._username = parsed.external_id
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[ContentRecord, str]:
        if self._username is None or self._cancellation is None:
            raise RuntimeError("X creator adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise _parse_failure("X creator checkpoint has an unsupported shape.")
        state = XTargetCursor.decode(cursor)
        self._cancellation.raise_if_cancelled()
        page = await self.provider.creator_posts(
            self._username,
            max_results=limit,
            pagination_token=state.next_token,
            until_id=state.until_id,
            exclude_replies=not bool(context.filters.get("include_replies", True)),
            exclude_reposts=not bool(context.filters.get("include_reposts", True)),
        )
        selected, next_state = _select_target_page(page, limit)
        return Page(
            tuple(normalize_x_post(post, self.pseudonymizer) for post in selected),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._username = None
        self._cancellation = None
        await self.provider.close()


class XCommentsAdapter:
    source_id = "x"
    provider_id = "x_api"
    owns_resources = True

    def __init__(
        self,
        provider: XReadProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._post_id: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url") or context.target.get("id")
        try:
            parsed = parse_x_target(str(target or ""))
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED, "X comments target is invalid."
            ) from exc
        if parsed.kind is not XTargetKind.POST:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "X comments target is not a conversation post.",
            )
        self._post_id = parsed.external_id
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[CommentRecord, str]:
        if self._post_id is None or self._cancellation is None:
            raise RuntimeError("X comments adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise _parse_failure("X comments checkpoint has an unsupported shape.")
        state = XTargetCursor.decode(cursor)
        self._cancellation.raise_if_cancelled()
        page = await self.provider.conversation_replies(
            self._post_id,
            max_results=limit,
            next_token=state.next_token,
            until_id=state.until_id,
        )
        selected, next_state = _select_target_page(page, limit)
        return Page(
            tuple(self._normalize_reply(post) for post in selected),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._post_id = None
        self._cancellation = None
        await self.provider.close()

    def _normalize_reply(self, post: XPost) -> CommentRecord:
        assert self._post_id is not None
        return CommentRecord(
            source_id="x",
            external_id=post.post_id,
            content_external_id=self._post_id,
            body=post.text[:4_000],
            author_pseudonym=self.pseudonymizer.pseudonym("x", post.author_id),
            published_at=post.created_at,
            like_count=max(0, int(post.metrics.get("like_count", 0))),
            child_count=max(0, int(post.metrics.get("reply_count", 0))),
            parent_external_id=post.parent_id or self._post_id,
            root_external_id=self._post_id,
            provenance={
                "provider_id": "x_api",
                "contract_version": "cbce.x.comment.v1",
                "coverage": "recent_conversation_replies",
            },
        )


def normalize_x_post(
    post: XPost, pseudonymizer: IdentityPseudonymizer
) -> ContentRecord:
    if not _POST_ID.fullmatch(post.post_id) or not post.text.strip():
        raise _parse_failure("X API post identity or text is invalid.")
    metrics = {
        key: value
        for key, value in post.metrics.items()
        if key in {"like_count", "reply_count", "repost_count", "quote_count"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    title = next(
        (line.strip() for line in post.text.splitlines() if line.strip()), post.text
    )[:180]
    return ContentRecord(
        source_id="x",
        external_id=post.post_id,
        canonical_url=post.canonical_url,
        title=title,
        body=post.text.strip()[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("x", post.author_id),
        published_at=post.created_at,
        metrics=metrics,
        media=post.media,
        provenance={
            "provider_id": "x_api",
            "contract_version": "cbce.x.post.v1",
            "coverage": "official_api",
        },
    )


def _select_target_page(
    page: XPostPage,
    limit: int,
) -> tuple[tuple[XPost, ...], XTargetCursor | None]:
    selected = page.items[:limit]
    if len(page.items) > len(selected) and selected:
        return selected, XTargetCursor(until_id=str(int(selected[-1].post_id) - 1))
    if page.next_token:
        return selected, XTargetCursor(next_token=page.next_token)
    return selected, None


def _parse_post_page(payload: Mapping[str, Any]) -> XPostPage:
    rows = payload.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise _parse_failure("X API post list has an invalid shape.")
    includes = payload.get("includes") or {}
    media_rows = includes.get("media") or [] if isinstance(includes, dict) else []
    media_by_key = {
        str(item.get("media_key")): item
        for item in media_rows
        if isinstance(item, dict) and item.get("media_key")
    }
    posts = tuple(_parse_post(row, media_by_key) for row in rows)
    meta = payload.get("meta") or {}
    token = str(meta.get("next_token") or "") if isinstance(meta, dict) else ""
    return XPostPage(posts, token or None)


def _parse_post(
    row: Any, media_by_key: Mapping[str, Mapping[str, Any]]
) -> XPost:
    if not isinstance(row, dict):
        raise _parse_failure("X API post has an invalid shape.")
    post_id = str(row.get("id") or "")
    text = str(row.get("text") or "").strip()
    if not _POST_ID.fullmatch(post_id) or not text:
        raise _parse_failure("X API post identity or text is missing.")
    created_at = _parse_datetime(row.get("created_at"))
    public = row.get("public_metrics") or {}
    metrics = {
        target: _nonnegative_int(public.get(source))
        for target, source in (
            ("like_count", "like_count"),
            ("reply_count", "reply_count"),
            ("repost_count", "retweet_count"),
            ("quote_count", "quote_count"),
        )
        if isinstance(public, dict)
    }
    entities = row.get("entities") or {}
    hashtags = tuple(
        f"#{str(item.get('tag')).strip()}"
        for item in (entities.get("hashtags") or [])
        if isinstance(item, dict) and str(item.get("tag") or "").strip()
    ) if isinstance(entities, dict) else ()
    attachments = row.get("attachments") or {}
    media_keys = attachments.get("media_keys") or [] if isinstance(attachments, dict) else []
    media = tuple(
        normalized
        for key in media_keys
        if (normalized := _parse_media(media_by_key.get(str(key)))) is not None
    )
    conversation_id = str(row.get("conversation_id") or "") or None
    references = row.get("referenced_tweets") or []
    parent_id = next(
        (
            str(item.get("id"))
            for item in references
            if isinstance(item, dict)
            and item.get("type") == "replied_to"
            and _POST_ID.fullmatch(str(item.get("id") or ""))
        ),
        None,
    )
    return XPost(
        post_id=post_id,
        text=text,
        author_id=str(row.get("author_id") or ""),
        created_at=created_at,
        language=str(row.get("lang") or "") or None,
        conversation_id=conversation_id,
        parent_id=parent_id,
        metrics=metrics,
        hashtags=hashtags,
        media=media,
    )


def _parse_media(item: Mapping[str, Any] | None) -> Mapping[str, object] | None:
    if not item:
        return None
    media_type = str(item.get("type") or "")
    url = str(item.get("url") or item.get("preview_image_url") or "")
    if media_type not in {"photo", "video", "animated_gif"} or not url.startswith(
        "https://"
    ):
        return None
    result: dict[str, object] = {"kind": media_type, "url": url}
    for key in ("width", "height"):
        value = _nonnegative_int(item.get(key))
        if value:
            result[key] = value
    duration = _nonnegative_int(item.get("duration_ms"))
    if duration:
        result["duration_ms"] = min(duration, 86_400_000)
    return result


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _response_failure(response: httpx.Response) -> CrawlerFailure:
    status = response.status_code
    problem_type = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            problem_type = str(payload.get("type") or "").casefold()
    except ValueError:
        pass
    if status == 401:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "X API credentials are invalid or expired.",
        )
    if status == 402 or "usage-capped" in problem_type:
        return CrawlerFailure(
            CrawlerErrorCode.PAYMENT_OR_ACCESS_REQUIRED,
            "X API credits or an eligible access tier are required for this operation.",
            details={"action": "OPEN_X_DEVELOPER_CONSOLE"},
        )
    if status == 403:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "The configured X application is not permitted to use this operation.",
            details={"action": "CHECK_X_APP_ACCESS"},
        )
    if status == 404:
        return CrawlerFailure(CrawlerErrorCode.NOT_FOUND, "X resource was not found.")
    if status == 429:
        reset = response.headers.get("x-rate-limit-reset")
        try:
            retry_after = max(0.0, float(reset) - time.time()) if reset else None
        except ValueError:
            retry_after = None
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "X API rate limit was reached.",
            retryable=True,
            retry_after_seconds=retry_after,
        )
    if status >= 500:
        return CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "X API is temporarily unavailable.",
            retryable=True,
        )
    return _parse_failure("X API rejected the request.")


def _parse_failure(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)
