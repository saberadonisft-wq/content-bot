"""Permission-gated Facebook Page feed through the official Graph API."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

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

_GRAPH_VERSION = re.compile(r"v[1-9][0-9]*\.[0-9]+")
_GRAPH_ID = re.compile(r"[1-9][0-9]{4,40}")
_POST_ID = re.compile(r"(?:[1-9][0-9]{4,40})(?:_[1-9][0-9]{4,40})?")
_PAGE_USERNAME = re.compile(r"[A-Za-z0-9.]{2,75}")
_CURSOR = re.compile(r"[A-Za-z0-9._=-]{1,2048}")
_FACEBOOK_HOSTS = frozenset(
    {"facebook.com", "www.facebook.com", "m.facebook.com"}
)


@dataclass(frozen=True, slots=True)
class MetaPageGraphConfig:
    api_version: str
    access_token: str = field(repr=False)
    page_id: str
    page_username: str
    base_url: str = "https://graph.facebook.com"

    def __post_init__(self) -> None:
        if not _GRAPH_VERSION.fullmatch(self.api_version):
            raise ValueError("Meta Graph API version must be explicitly pinned")
        if not self.access_token.strip() or len(self.access_token) > 4096:
            raise ValueError("Facebook Page access token is missing or invalid")
        if not _GRAPH_ID.fullmatch(self.page_id):
            raise ValueError("Facebook Page ID is invalid")
        if not _PAGE_USERNAME.fullmatch(self.page_username):
            raise ValueError("Facebook Page username is invalid")
        if self.base_url != "https://graph.facebook.com":
            raise ValueError("Meta Graph API host is not approved")


@dataclass(frozen=True, slots=True)
class FacebookPagePost:
    post_id: str
    permalink: str
    message: str = ""
    author_id: str = ""
    published_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FacebookPagePostPage:
    items: tuple[FacebookPagePost, ...]
    next_after: str | None


@dataclass(frozen=True, slots=True)
class FacebookPageCursor:
    after: str = ""

    def __post_init__(self) -> None:
        if self.after and not _CURSOR.fullmatch(self.after):
            raise ValueError("Facebook Graph cursor is invalid")

    def encode(self) -> str:
        payload = json.dumps(
            [self.after], ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
        return "v1f:" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> FacebookPageCursor:
        if value is None:
            return cls()
        if not value.startswith("v1f:") or len(value) > 4_096:
            raise _parse_failure("Facebook Page checkpoint is invalid.")
        encoded = value[4:]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            decoded = json.loads(raw)
            if not isinstance(decoded, list) or len(decoded) != 1:
                raise ValueError
            return cls(str(decoded[0]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise _parse_failure("Facebook Page checkpoint is invalid.") from exc


class FacebookPageFeedProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def page_feed(
        self, *, after: str | None, limit: int
    ) -> FacebookPagePostPage: ...

    async def close(self) -> None: ...


class FacebookGraphPageProvider:
    """Official Page transport with no browser/cookie fallback."""

    _FIELDS = (
        "id,message,created_time,permalink_url,from{id},shares,"
        "reactions.limit(0).summary(true),comments.limit(0).summary(true)"
    )

    def __init__(
        self,
        config: MetaPageGraphConfig,
        client: httpx.AsyncClient | None = None,
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

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if (
            context.source_id != "facebook"
            or context.provider_id != "meta_pages"
            or context.operation != "scan_channel"
        ):
            raise ValueError("Facebook Page provider context is invalid")
        self._cancellation = cancellation
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.config.base_url}/{self.config.api_version}",
                headers={"Authorization": f"Bearer {self.config.access_token}"},
                timeout=30,
                follow_redirects=False,
            )

    async def page_feed(
        self, *, after: str | None, limit: int
    ) -> FacebookPagePostPage:
        if not 1 <= limit <= 100:
            raise ValueError("Facebook Page feed limit is invalid")
        params: dict[str, str | int] = {
            "fields": self._FIELDS,
            "limit": limit,
        }
        if after:
            if not _CURSOR.fullmatch(after):
                raise ValueError("Facebook Graph cursor is invalid")
            params["after"] = after
        payload = await self._get(f"/{self.config.page_id}/feed", params=params)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise _parse_failure("Facebook Page feed response changed.")
        items = tuple(
            post for row in rows if (post := _parse_post(row)) is not None
        )
        paging = payload.get("paging")
        cursors = paging.get("cursors") if isinstance(paging, Mapping) else None
        next_after = (
            str(cursors.get("after") or "")
            if isinstance(cursors, Mapping) and paging.get("next")
            else ""
        )
        if next_after and not _CURSOR.fullmatch(next_after):
            raise _parse_failure("Facebook Page pagination cursor changed.")
        return FacebookPagePostPage(items, next_after or None)

    async def close(self) -> None:
        client, self._client = self._client, None
        self._cancellation = None
        if client is not None and self._owns_client:
            await client.aclose()

    async def _get(
        self, path: str, *, params: Mapping[str, str | int]
    ) -> Mapping[str, Any]:
        if self._client is None or self._cancellation is None:
            raise RuntimeError("Facebook Graph provider is not open")
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
                "Facebook Graph API request failed.",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise _response_failure(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _parse_failure("Facebook Graph API returned invalid JSON.") from exc
        if not isinstance(payload, Mapping):
            raise _parse_failure("Facebook Graph API returned an invalid response.")
        return payload


class FacebookPageFeedAdapter:
    source_id = "facebook"
    provider_id = "meta_pages"
    owns_resources = True

    def __init__(
        self,
        provider: FacebookPageFeedProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[ContentRecord, str]:
        del context
        if self._cancellation is None:
            raise RuntimeError("Facebook Page adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise _parse_failure("Facebook Page checkpoint shape is invalid.")
        state = FacebookPageCursor.decode(cursor)
        self._cancellation.raise_if_cancelled()
        page = await self.provider.page_feed(
            after=state.after or None,
            limit=min(limit, 100),
        )
        records = tuple(
            normalize_facebook_page_post(item, self.pseudonymizer)
            for item in page.items[:limit]
        )
        next_cursor = (
            FacebookPageCursor(page.next_after).encode()
            if page.next_after
            else None
        )
        return Page(records, next_cursor, next_cursor is not None)

    async def close(self) -> None:
        self._cancellation = None
        await self.provider.close()


def normalize_facebook_page_post(
    post: FacebookPagePost,
    pseudonymizer: IdentityPseudonymizer,
) -> ContentRecord:
    if not _POST_ID.fullmatch(post.post_id):
        raise _parse_failure("Facebook Page post identity is invalid.")
    canonical_url = _canonical_permalink(post.permalink)
    message = post.message.strip()
    title = next(
        (line.strip() for line in message.splitlines() if line.strip()),
        f"Facebook Page post {post.post_id.rsplit('_', 1)[-1]}",
    )[:180]
    published_at = post.published_at
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    metrics = {
        key: value
        for key, value in post.metrics.items()
        if key in {"reaction_count", "comment_count", "share_count"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return ContentRecord(
        source_id="facebook",
        external_id=post.post_id,
        canonical_url=canonical_url,
        title=title,
        body=message[:4_000],
        author_pseudonym=pseudonymizer.pseudonym(
            "facebook", post.author_id
        ),
        published_at=published_at,
        metrics=metrics,
        provenance={
            "provider_id": "meta_pages",
            "contract_version": "cbce.facebook.page-feed.v1",
            "access_basis": "authorized_page",
            "coverage": "partial",
        },
    )


def _parse_post(value: Any) -> FacebookPagePost | None:
    if not isinstance(value, Mapping):
        return None
    post_id = str(value.get("id") or "")
    permalink = str(value.get("permalink_url") or "")
    if not _POST_ID.fullmatch(post_id) or not permalink:
        return None
    published_at = None
    if value.get("created_time"):
        try:
            published_at = datetime.fromisoformat(str(value["created_time"]))
        except ValueError:
            published_at = None
    author = value.get("from")
    shares = value.get("shares")
    return FacebookPagePost(
        post_id=post_id,
        permalink=permalink,
        message=str(value.get("message") or "")[:10_000],
        author_id=(
            str(author.get("id") or "")[:80]
            if isinstance(author, Mapping)
            else ""
        ),
        published_at=published_at,
        metrics={
            "reaction_count": _summary_count(value.get("reactions")),
            "comment_count": _summary_count(value.get("comments")),
            "share_count": _safe_count(
                shares.get("count") if isinstance(shares, Mapping) else 0
            ),
        },
    )


def _summary_count(value: Any) -> int:
    summary = value.get("summary") if isinstance(value, Mapping) else None
    return _safe_count(
        summary.get("total_count") if isinstance(summary, Mapping) else 0
    )


def _safe_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _canonical_permalink(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.port
        or host not in _FACEBOOK_HOSTS
        or not parsed.path.startswith("/")
    ):
        raise _parse_failure("Facebook Page permalink is invalid.")
    return urlunsplit(("https", "www.facebook.com", parsed.path, "", ""))


def _response_failure(response: httpx.Response) -> CrawlerFailure:
    status = response.status_code
    error_code = None
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, Mapping) else None
        if isinstance(error, Mapping) and error.get("code") is not None:
            error_code = int(error["code"])
    except (ValueError, TypeError):
        pass
    if status == 401 or error_code == 190:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "Facebook Page access token is invalid or expired.",
        )
    if status == 403 or error_code in {10, 200}:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "Facebook Page permission, Page task, or App Review access is required.",
        )
    if status == 429 or error_code in {4, 17, 32, 613}:
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "Facebook Graph API rate limit was reached.",
            retryable=True,
        )
    if status == 404:
        return CrawlerFailure(
            CrawlerErrorCode.NOT_FOUND,
            "Facebook Page resource was not found.",
        )
    if status >= 500:
        return CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "Facebook Graph API is temporarily unavailable.",
            retryable=True,
        )
    return CrawlerFailure(
        CrawlerErrorCode.PERMISSION_REQUIRED,
        "Facebook Graph request was rejected.",
    )


def _parse_failure(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)
