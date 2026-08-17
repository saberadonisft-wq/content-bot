from __future__ import annotations

import abc
import asyncio
import hashlib
import html
import json
import logging
import os
import shlex
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

from ..config import settings
from ..crawlers.adapters.bluesky import (
    BlueskyApiCommentProvider,
    BlueskyCommentBudgets,
    BlueskyCommentsAdapter,
    BlueskyCommentScan,
)
from ..crawlers.adapters.facebook import (
    FacebookGraphPageProvider,
    FacebookPageFeedAdapter,
    FacebookTargetKind,
    MetaPageGraphConfig,
    parse_facebook_target,
)
from ..crawlers.adapters.instagram import (
    InstagramGraphHashtagProvider,
    InstagramHashtagBudget,
    InstagramHashtagSearchAdapter,
    MetaGraphConfig,
)
from ..crawlers.adapters.mastodon import (
    MastodonApiCommentProvider,
    MastodonCommentBudgets,
    MastodonCommentsAdapter,
    MastodonCommentScan,
    parse_mastodon_status_target,
)
from ..crawlers.adapters.reddit import (
    RedditApiCommentProvider,
    RedditCommentBudgets,
    RedditCommentsAdapter,
    RedditCommentScan,
)
from ..crawlers.adapters.tiktok import (
    TikTokDisplayApiProvider,
    TikTokDisplayConfig,
    TikTokDisplayDetailAdapter,
    TikTokDisplayVideoListAdapter,
    TikTokOAuthClient,
    TikTokOAuthConfig,
    TikTokTargetKind,
    TikTokTokenVault,
    parse_tiktok_target,
)
from ..crawlers.adapters.x import (
    XApiClient,
    XCommentsAdapter,
    XCreatorAdapter,
    XDetailAdapter,
    XSearchAdapter,
    XTargetKind,
    parse_x_target,
)
from ..crawlers.adapters.youtube import (
    YouTubeApiCommentProvider,
    YouTubeCommentBudgets,
    YouTubeCommentsAdapter,
    YouTubeCommentScan,
)
from ..crawlers.runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    PseudonymKeyStore,
    RunBudgets,
    RunContext,
    bounded_pages,
)
from .bluesky_api import (
    bluesky_json,
    minimized_post_payload,
)
from .bluesky_api import (
    extract_tags as bluesky_tags,
)
from .bluesky_api import (
    is_invalid_cursor as bluesky_invalid_cursor,
)
from .bluesky_api import (
    parse_datetime as bluesky_datetime,
)
from .bluesky_api import (
    post_identity as bluesky_post_identity,
)
from .feed_ingestion import (
    fetch_feed_document,
    parse_feed_templates,
    safe_entry_url,
)
from .mastodon_api import (
    MastodonRequestBudget,
    mastodon_instances,
    mastodon_json,
)
from .mastodon_api import (
    next_max_id as mastodon_next_max_id,
)
from .mastodon_api import (
    normalize_hashtag as mastodon_hashtag,
)
from .mastodon_api import (
    normalize_status as normalize_mastodon_status,
)
from .reddit_oauth import reddit_token_cache
from .steam_reviews import (
    SteamRequestBudget,
    select_discovered_apps,
    steam_review_external_id,
    steam_review_payload,
)
from .youtube_api import YouTubeQuotaBudget, is_invalid_page_token, youtube_json

DEFAULT_WEB_FEED_URLS = "https://news.google.com/rss/search?q={query}&hl=vi&gl=VN&ceid=VN:vi"
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
            response = await client.get(url, params=params) if params is not None else await client.get(url)
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
            delay = min(max(float(retry_after), 0), 5) if retry_after else 0.5 * (2**attempt)
        except ValueError:
            delay = 0.5 * (2**attempt)
        await asyncio.sleep(delay)
    raise RuntimeError("HTTP retry loop ended unexpectedly")


@dataclass(frozen=True)
class ConnectorCapabilities:
    global_search: bool
    watchlist_filter: bool = False
    requires_login: bool = False
    interaction_fields: tuple[str, ...] = ()


@dataclass
class ConnectorStatus:
    state: str
    detail: str
    reason_code: str | None = None
    probe: str = "local"


@dataclass
class SearchQuery:
    keyword_id: int
    name: str
    include_terms: list[str]
    max_items: int
    request_budget: int | None = None
    deadline_seconds: float | None = None
    progress_callback: Callable[[str, str], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )
    warning_callback: Callable[[str, str], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )
    checkpoint_tracker: Any | None = field(default=None, repr=False)
    legacy_checkpoint: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.max_items <= 0:
            raise ValueError("Search item budget must be positive")
        if self.request_budget is not None and self.request_budget <= 0:
            raise ValueError("Search request budget must be positive")
        if self.deadline_seconds is not None and self.deadline_seconds <= 0:
            raise ValueError("Search deadline must be positive")

    def request_limit(self, default: int, *, maximum: int) -> int:
        selected = default
        if self.request_budget is not None:
            selected = min(selected, self.request_budget)
        return max(1, min(selected, maximum))

    def deadline_limit(self, default: float, *, maximum: float = 7_200) -> float:
        selected = default
        if self.deadline_seconds is not None:
            selected = min(selected, self.deadline_seconds)
        return max(1.0, min(selected, maximum))

    @property
    def search_terms(self) -> list[str]:
        seen: set[str] = set()
        terms: list[str] = []
        for term in (self.name, *self.include_terms):
            cleaned = term.strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                terms.append(cleaned)
        return terms

    def resume_cursor(
        self,
        kind: str,
        *,
        default: Any = None,
        legacy_key: str | None = None,
        **scope: Any,
    ) -> Any:
        if self.checkpoint_tracker is not None:
            from .checkpoints import cursor_scope

            value = self.checkpoint_tracker.cursor(cursor_scope(kind, **scope), default=None)
            if value is not None:
                return value
        if legacy_key:
            return self.legacy_checkpoint.get(legacy_key, default)
        return default

    def report_cursor(self, kind: str, value: Any | None, **scope: Any) -> None:
        if self.checkpoint_tracker is None:
            return
        from .checkpoints import cursor_scope

        self.checkpoint_tracker.report_cursor(cursor_scope(kind, **scope), value)

    @property
    def recent_ids(self) -> tuple[str, ...]:
        if self.checkpoint_tracker is None:
            return tuple(str(item) for item in self.legacy_checkpoint.get("recent_ids", []))
        return tuple(self.checkpoint_tracker.recent_ids)


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


@dataclass
class RawContentItem:
    external_id: str
    canonical_url: str
    title: str
    body_snippet: str = ""
    author: str = ""
    hashtags: list[str] = field(default_factory=list)
    locale: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, int] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)


class SourceConnector(abc.ABC):
    source_id: str
    label: str
    group: str
    capabilities: ConnectorCapabilities

    @property
    def configured(self) -> bool:
        """Return whether the connector has its local prerequisites.

        This is intentionally a synchronous, side-effect-free signal for run
        planning and catalog defaults. Remote health remains the responsibility
        of ``healthcheck``.
        """
        return True

    @abc.abstractmethod
    async def healthcheck(self) -> ConnectorStatus:
        raise NotImplementedError

    async def deep_healthcheck(self) -> ConnectorStatus:
        """Perform an explicit remote probe when the caller accepts its cost."""
        return await self.healthcheck()

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Declare provider options that change cursor compatibility."""
        del operation, channel
        return {}

    @abc.abstractmethod
    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class XCommentScan:
    content_external_id: str
    records: tuple[CommentRecord, ...]
    root_count: int
    child_count: int
    request_count: int
    truncated: bool
    provider_id: str = "x_api"


class XConnector(SourceConnector):
    """Read-only official X recent-search connector."""

    source_id = "x"
    label = "X"
    group = "Official API"
    capabilities = ConnectorCapabilities(
        True,
        watchlist_filter=True,
        interaction_fields=(
            "like_count",
            "reply_count",
            "repost_count",
            "quote_count",
        ),
    )

    @property
    def configured(self) -> bool:
        return bool(settings.x_bearer_token)

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.x_bearer_token:
            return ConnectorStatus(
                "not_configured",
                "Add X_BEARER_TOKEN and API credits/access in backend/.env to enable official read-only search.",
            )
        return ConnectorStatus(
            "ready",
            "Official X API credentials are configured; access and credits are checked on each operation.",
        )

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        token = settings.x_bearer_token
        if not token:
            return
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        initial_cursor = query.resume_cursor(
            "query", default=None, legacy_key="cursor"
        )
        context = RunContext(
            run_id=f"x-keyword-{query.keyword_id}",
            keyword_id=query.keyword_id,
            source_id="x",
            provider_id="x_api",
            operation="search",
            target={"kind": "keyword"},
            terms=tuple(query.search_terms),
            filters={},
            budgets=RunBudgets(
                max_items=query.max_items,
                max_requests=query.request_limit(100, maximum=100),
                deadline_seconds=query.deadline_limit(900),
            ),
        )
        adapter = XSearchAdapter(
            self._provider(token),
            self._pseudonymizer(),
        )
        cancellation = CancellationToken()
        await adapter.open(context, cancellation)
        try:
            async def fetch(cursor: object | None, limit: int):
                return await adapter.fetch_page(context, cursor, limit)

            async for batch in bounded_pages(
                fetch,
                budgets=context.budgets,
                cancellation=cancellation,
                initial_cursor=initial_cursor,
                page_size=100,
            ):
                for record in batch.items:
                    yield self._raw_item(record)
                query.report_cursor("query", batch.next_cursor)
        finally:
            await adapter.close()

    async def scan_channel(
        self,
        channel: dict[str, Any],
        query: SearchQuery,
    ) -> AsyncIterator[RawContentItem]:
        token = settings.x_bearer_token
        if not token:
            raise RuntimeError(
                "X API credentials and credits are required to scan a saved account"
            )
        target_url = str(channel.get("normalized_url") or channel.get("url") or "")
        try:
            target = parse_x_target(target_url)
        except ValueError as exc:
            raise RuntimeError("X saved channel target is invalid") from exc
        if target.kind is not XTargetKind.CREATOR:
            raise RuntimeError("X saved channels must identify a creator account")
        username = target.external_id
        scope_username = username.casefold()
        initial_cursor = query.resume_cursor(
            "channel",
            default=None,
            username=scope_username,
        )
        context = RunContext(
            run_id=f"x-channel-{query.keyword_id}",
            keyword_id=query.keyword_id,
            source_id="x",
            provider_id="x_api",
            operation="scan_channel",
            target={"kind": "creator", "url": target.canonical_url},
            terms=(),
            filters={
                "include_replies": bool(channel.get("include_replies", False)),
                "include_reposts": bool(channel.get("include_reposts", False)),
            },
            budgets=RunBudgets(
                max_items=query.max_items,
                max_requests=query.request_limit(100, maximum=100),
                deadline_seconds=query.deadline_limit(900),
            ),
        )
        adapter = XCreatorAdapter(self._provider(token), self._pseudonymizer())
        cancellation = CancellationToken()
        await adapter.open(context, cancellation)
        try:
            async def fetch(cursor: object | None, limit: int):
                return await adapter.fetch_page(context, cursor, limit)

            async for batch in bounded_pages(
                fetch,
                budgets=context.budgets,
                cancellation=cancellation,
                initial_cursor=initial_cursor,
                page_size=100,
            ):
                for record in batch.items:
                    yield self._raw_item(record)
                query.report_cursor(
                    "channel",
                    batch.next_cursor,
                    username=scope_username,
                )
        finally:
            await adapter.close()

    async def fetch_detail(self, target_url: str) -> RawContentItem:
        token = settings.x_bearer_token
        if not token:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "X API credentials are required to fetch a post.",
            )
        context = RunContext(
            run_id="x-detail",
            keyword_id=0,
            source_id="x",
            provider_id="x_api",
            operation="fetch_detail",
            target={"kind": "content_url", "url": target_url},
            terms=(),
            filters={},
            budgets=RunBudgets(max_items=1, max_requests=1, deadline_seconds=60),
        )
        adapter = XDetailAdapter(self._provider(token), self._pseudonymizer())
        await adapter.open(context, CancellationToken())
        try:
            return self._raw_item(await adapter.fetch())
        finally:
            await adapter.close()

    async def list_creator(
        self,
        target_url: str,
        *,
        max_items: int = 100,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[RawContentItem]:
        token = settings.x_bearer_token
        if not token:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "X API credentials are required to list creator posts.",
            )
        item_limit = min(max(int(max_items), 1), 1_000)
        context = RunContext(
            run_id="x-creator",
            keyword_id=0,
            source_id="x",
            provider_id="x_api",
            operation="list_creator",
            target={"kind": "creator", "url": target_url},
            terms=(),
            filters={"include_replies": True, "include_reposts": True},
            budgets=RunBudgets(
                max_items=item_limit,
                max_requests=min(100, max(1, (item_limit + 99) // 100)),
                deadline_seconds=300,
            ),
        )
        adapter = XCreatorAdapter(self._provider(token), self._pseudonymizer())
        cancellation = CancellationToken()
        await adapter.open(context, cancellation)
        try:
            async def fetch(cursor: object | None, limit: int):
                return await adapter.fetch_page(context, cursor, limit)

            async for batch in bounded_pages(
                fetch,
                budgets=context.budgets,
                cancellation=cancellation,
                initial_cursor=initial_cursor,
                page_size=100,
            ):
                for record in batch.items:
                    yield self._raw_item(record)
        finally:
            await adapter.close()

    async def scan_comments(
        self,
        target_url: str,
        budgets: RunBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
    ) -> XCommentScan:
        if sort not in {"new", "provider"}:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "X conversation replies support provider/new ordering only.",
            )
        token = settings.x_bearer_token
        if not token:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "X API credentials are required to list conversation replies.",
            )
        total_limit = min(
            budgets.max_total_comments or budgets.max_items,
            budgets.max_items,
        )
        context = RunContext(
            run_id="x-comments",
            keyword_id=0,
            source_id="x",
            provider_id="x_api",
            operation="list_comments",
            target={"kind": "content_url", "url": target_url},
            terms=(),
            filters={"ordering": "provider_newest"},
            budgets=RunBudgets(
                max_items=total_limit,
                max_requests=budgets.max_requests,
                deadline_seconds=budgets.deadline_seconds,
                max_root_comments=budgets.max_root_comments,
                max_children_per_root=budgets.max_children_per_root,
                max_total_comments=total_limit,
            ),
        )
        content_external_id = _x_content_id(target_url)
        adapter = XCommentsAdapter(self._provider(token), self._pseudonymizer())
        token_cancel = cancellation or CancellationToken()
        await adapter.open(context, token_cancel)
        records: list[CommentRecord] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        requests = 0
        has_more = False
        try:
            async with asyncio.timeout(context.budgets.deadline_seconds):
                while len(records) < total_limit and requests < budgets.max_requests:
                    token_cancel.raise_if_cancelled()
                    page = await adapter.fetch_page(
                        context,
                        cursor,
                        min(100, total_limit - len(records)),
                    )
                    requests += 1
                    records.extend(page.items)
                    has_more = page.has_more
                    if not page.has_more:
                        break
                    next_cursor = page.next_cursor
                    if (
                        not next_cursor
                        or next_cursor == cursor
                        or next_cursor in seen_cursors
                    ):
                        raise CrawlerFailure(
                            CrawlerErrorCode.CURSOR_STALLED,
                            "X conversation reply cursor did not advance.",
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except TimeoutError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.DEADLINE_EXCEEDED,
                "X conversation reply deadline was exceeded.",
                retryable=True,
            ) from exc
        finally:
            await adapter.close()
        ordered, unresolved = _order_x_comments(
            records[:total_limit],
            content_external_id=content_external_id,
        )
        root_count = sum(
            record.parent_external_id in {None, record.content_external_id}
            for record in ordered
        )
        return XCommentScan(
            content_external_id=content_external_id,
            records=ordered,
            root_count=root_count,
            child_count=len(ordered) - root_count,
            request_count=requests,
            truncated=bool(
                unresolved
                or has_more
            ),
        )

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 100,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[CommentRecord]:
        if initial_cursor:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "X manual comment scans do not accept a durable cursor.",
            )
        total = min(max(int(max_items), 1), 1_000)
        scan = await self.scan_comments(
            target_url,
            RunBudgets(
                max_items=total,
                max_requests=min(100, max(1, (total + 99) // 100)),
                deadline_seconds=300,
                max_root_comments=total,
                max_children_per_root=total,
                max_total_comments=total,
            ),
        )
        for record in scan.records:
            yield record

    @staticmethod
    def _provider(token: str) -> XApiClient:
        return XApiClient(token)

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())

    @staticmethod
    def _raw_item(record) -> RawContentItem:
        return RawContentItem(
            external_id=record.external_id,
            canonical_url=record.canonical_url,
            title=record.title,
            body_snippet=record.body,
            author=record.author_pseudonym,
            published_at=record.published_at,
            metrics=dict(record.metrics),
            raw_payload={
                "provider_id": "x_api",
                "media": [dict(item) for item in record.media],
            },
        )


def _x_content_id(target_url: str) -> str:
    try:
        target = parse_x_target(target_url)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "X conversation target is invalid.",
        ) from exc
    if target.kind is not XTargetKind.POST:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "X conversation target is not a post.",
        )
    return target.external_id


def _order_x_comments(
    records: list[CommentRecord],
    *,
    content_external_id: str,
) -> tuple[tuple[CommentRecord, ...], int]:
    pending: dict[str, CommentRecord] = {}
    for record in records:
        if record.external_id in pending:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "X conversation replies contained duplicate identities.",
            )
        pending[record.external_id] = record
    available = {content_external_id}
    ordered: list[CommentRecord] = []
    while pending:
        ready = [
            record
            for record in pending.values()
            if record.parent_external_id is None
            or record.parent_external_id in available
        ]
        if not ready:
            break
        for record in ready:
            pending.pop(record.external_id)
            available.add(record.external_id)
            ordered.append(record)
    return tuple(ordered), len(pending)


class TikTokDisplayConnector(SourceConnector):
    """Official Display API for one explicitly authorized TikTok creator."""

    source_id = "tiktok"
    label = "TikTok"
    group = "Approved API"
    capabilities = ConnectorCapabilities(
        False,
        watchlist_filter=True,
        interaction_fields=(
            "like_count",
            "comment_count",
            "share_count",
            "view_count",
        ),
    )

    def __init__(self) -> None:
        self._refresh_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        stored = self._stored_credential()
        if stored is not None:
            username = stored.authorized_username or self._settings_username()
            can_refresh = not stored.refresh_expired and self._oauth_configured()
            return bool(
                username
                and {
                    "user.info.basic",
                    "user.info.profile",
                    "video.list",
                }.issubset(stored.scopes)
                and (not stored.access_expired or can_refresh)
            )
        return bool(
            settings.tiktok_user_access_token
            and settings.tiktok_open_id
            and settings.tiktok_authorized_username
            and "video.list" in self._granted_scopes()
        )

    async def healthcheck(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(
                "not_configured",
                "Complete approved TikTok Login Kit OAuth with video.list for one creator account.",
            )
        return ConnectorStatus(
            "ready",
            "TikTok Display API is configured for the explicitly authorized creator account.",
        )

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        del query, checkpoint
        if False:
            yield RawContentItem("", "", "")

    async def scan_channel(
        self,
        channel: dict[str, Any],
        query: SearchQuery,
    ) -> AsyncIterator[RawContentItem]:
        target_url = str(channel.get("normalized_url") or channel.get("url") or "")
        try:
            target = parse_tiktok_target(target_url)
        except ValueError as exc:
            raise RuntimeError("TikTok channel URL is invalid") from exc
        expected_username = self._authorized_username()
        if (
            target.kind is not TikTokTargetKind.CREATOR
            or target.external_id.casefold() != expected_username.casefold()
        ):
            raise RuntimeError(
                "TikTok Display API can scan only the creator account that authorized this connection"
            )
        if not self.configured:
            raise RuntimeError("TikTok Display API OAuth is not configured")
        initial_cursor = query.resume_cursor(
            "channel", default=None, username=expected_username.casefold()
        )
        display_config = await self._display_config()
        context = RunContext(
            run_id=f"tiktok-channel-{query.keyword_id}",
            keyword_id=query.keyword_id,
            source_id="tiktok",
            provider_id="tiktok_display",
            operation="scan_channel",
            target={
                "kind": "authorized_account",
                "open_id": display_config.open_id,
                "username": expected_username,
            },
            terms=(),
            filters={"access_basis": "authorized_creator"},
            budgets=RunBudgets(
                max_items=query.max_items,
                max_requests=query.request_limit(
                    max(1, (query.max_items + 19) // 20), maximum=100
                ),
                deadline_seconds=query.deadline_limit(300),
            ),
        )
        adapter = TikTokDisplayVideoListAdapter(
            self._provider(display_config),
            self._pseudonymizer(),
            display_config.open_id,
        )
        cancellation = CancellationToken()
        await adapter.open(context, cancellation)
        try:
            async def fetch(cursor: object | None, limit: int):
                return await adapter.fetch_page(context, cursor, limit)

            async for batch in bounded_pages(
                fetch,
                budgets=context.budgets,
                cancellation=cancellation,
                initial_cursor=initial_cursor,
                page_size=min(20, query.max_items),
            ):
                for record in batch.items:
                    yield self._raw_item(record)
                query.report_cursor(
                    "channel",
                    batch.next_cursor,
                    username=expected_username.casefold(),
                )
        finally:
            await adapter.close()

    async def fetch_detail(self, target_url: str) -> RawContentItem:
        if not self.configured:
            raise RuntimeError("TikTok Display API OAuth is not configured")
        display_config = await self._display_config()
        context = RunContext(
            run_id="tiktok-display-detail",
            keyword_id=0,
            source_id="tiktok",
            provider_id="tiktok_display",
            operation="fetch_detail",
            target={"kind": "content_url", "url": target_url},
            terms=(),
            filters={"access_basis": "authorized_creator"},
            budgets=RunBudgets(max_items=1, max_requests=1, deadline_seconds=60),
        )
        adapter = TikTokDisplayDetailAdapter(
            self._provider(display_config),
            self._pseudonymizer(),
            display_config.open_id,
        )
        await adapter.open(context, CancellationToken())
        try:
            return self._raw_item(await adapter.fetch())
        finally:
            await adapter.close()

    @staticmethod
    def _provider(config: TikTokDisplayConfig) -> TikTokDisplayApiProvider:
        return TikTokDisplayApiProvider(config)

    async def _display_config(self) -> TikTokDisplayConfig:
        stored = self._stored_credential()
        if stored is not None:
            if datetime.now(UTC) + timedelta(minutes=5) >= stored.access_expires_at:
                async with self._refresh_lock:
                    stored = self._stored_credential()
                    if stored is None:
                        raise RuntimeError("TikTok OAuth credential disappeared")
                    if datetime.now(UTC) + timedelta(minutes=5) >= stored.access_expires_at:
                        if stored.refresh_expired:
                            raise RuntimeError("TikTok OAuth refresh token has expired")
                        if not self._oauth_configured():
                            raise RuntimeError(
                                "TikTok OAuth client settings are required to refresh access"
                            )
                        oauth = TikTokOAuthClient(
                            TikTokOAuthConfig(
                                settings.tiktok_client_key,
                                settings.tiktok_client_secret or "",
                                settings.tiktok_redirect_uri,
                            )
                        )
                        try:
                            refreshed = await oauth.refresh(stored.refresh_token)
                        finally:
                            await oauth.close()
                        stored = self._vault().save(
                            refreshed,
                            authorized_username=(
                                stored.authorized_username or self._settings_username()
                            ),
                        )
            return stored.display_config()
        token = settings.tiktok_user_access_token or ""
        return TikTokDisplayConfig(
            settings.tiktok_open_id,
            self._granted_scopes(),
            token,
        )

    @staticmethod
    def _vault() -> TikTokTokenVault:
        return TikTokTokenVault(settings.data_dir / "crawler-secrets" / "tiktok")

    def _stored_credential(self):
        try:
            return self._vault().load()
        except ValueError:
            return None

    def _authorized_username(self) -> str:
        stored = self._stored_credential()
        return (
            stored.authorized_username if stored is not None else ""
        ) or self._settings_username()

    @staticmethod
    def _settings_username() -> str:
        return settings.tiktok_authorized_username.strip().removeprefix("@").casefold()

    @staticmethod
    def _oauth_configured() -> bool:
        return bool(
            settings.tiktok_client_key
            and settings.tiktok_client_secret
            and settings.tiktok_redirect_uri
        )

    @staticmethod
    def _granted_scopes() -> frozenset[str]:
        return frozenset(
            scope.strip()
            for scope in settings.tiktok_granted_scopes.split(",")
            if scope.strip()
        )

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())

    @staticmethod
    def _raw_item(record) -> RawContentItem:
        return RawContentItem(
            external_id=record.external_id,
            canonical_url=record.canonical_url,
            title=record.title,
            body_snippet=record.body,
            author=record.author_pseudonym,
            published_at=record.published_at,
            metrics=dict(record.metrics),
            raw_payload={
                "provider_id": "tiktok_display",
                "access_basis": "authorized_creator",
            },
        )


class FacebookPageConnector(SourceConnector):
    """Official Page feed for one explicitly authorized Facebook Page."""

    source_id = "facebook"
    label = "Facebook"
    group = "Approved API"
    capabilities = ConnectorCapabilities(
        False,
        watchlist_filter=True,
        interaction_fields=("reaction_count", "comment_count", "share_count"),
    )

    @property
    def configured(self) -> bool:
        try:
            self._config()
        except ValueError:
            return False
        return True

    async def healthcheck(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(
                "not_configured",
                "Configure a pinned Graph version, authorized Page access token, Page ID, and Page username.",
            )
        return ConnectorStatus(
            "ready",
            "Official Facebook Page feed is configured for the explicitly authorized Page.",
        )

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        del query, checkpoint
        if False:
            yield RawContentItem("", "", "")

    async def scan_channel(
        self,
        channel: dict[str, Any],
        query: SearchQuery,
    ) -> AsyncIterator[RawContentItem]:
        if not self.configured:
            raise RuntimeError("Facebook Page API is not configured")
        config = self._config()
        target_url = str(channel.get("normalized_url") or channel.get("url") or "")
        try:
            target = parse_facebook_target(target_url)
        except ValueError as exc:
            raise RuntimeError("Facebook Page URL is invalid") from exc
        approved_targets = {config.page_id.casefold(), config.page_username.casefold()}
        if (
            target.kind is not FacebookTargetKind.PAGE
            or target.external_id.casefold() not in approved_targets
        ):
            raise RuntimeError(
                "Facebook Pages API can scan only the explicitly authorized Page"
            )
        initial_cursor = query.resume_cursor(
            "channel", default=None, page_id=config.page_id
        )
        context = RunContext(
            run_id=f"facebook-page-{query.keyword_id}",
            keyword_id=query.keyword_id,
            source_id="facebook",
            provider_id="meta_pages",
            operation="scan_channel",
            target={"kind": "page", "page_id": config.page_id},
            terms=(),
            filters={"access_basis": "authorized_page"},
            budgets=RunBudgets(
                max_items=query.max_items,
                max_requests=query.request_limit(
                    max(1, min(100, (query.max_items + 99) // 100)), maximum=100
                ),
                deadline_seconds=query.deadline_limit(300),
            ),
        )
        adapter = FacebookPageFeedAdapter(
            self._provider(config),
            self._pseudonymizer(),
        )
        cancellation = CancellationToken()
        await adapter.open(context, cancellation)
        try:
            async def fetch(cursor: object | None, limit: int):
                page = await adapter.fetch_page(context, cursor, limit)
                query.report_cursor(
                    "channel", page.next_cursor, page_id=config.page_id
                )
                return page

            async for batch in bounded_pages(
                fetch,
                budgets=context.budgets,
                cancellation=cancellation,
                initial_cursor=initial_cursor,
                page_size=min(100, query.max_items),
            ):
                for record in batch.items:
                    yield self._raw_item(record)
        finally:
            await adapter.close()

    @staticmethod
    def _config() -> MetaPageGraphConfig:
        return MetaPageGraphConfig(
            settings.meta_graph_api_version,
            settings.facebook_page_access_token or "",
            settings.facebook_page_id,
            settings.facebook_page_username,
        )

    @staticmethod
    def _provider(config: MetaPageGraphConfig) -> FacebookGraphPageProvider:
        return FacebookGraphPageProvider(config)

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())

    @staticmethod
    def _raw_item(record) -> RawContentItem:
        return RawContentItem(
            external_id=record.external_id,
            canonical_url=record.canonical_url,
            title=record.title,
            body_snippet=record.body,
            author=record.author_pseudonym,
            published_at=record.published_at,
            metrics=dict(record.metrics),
            raw_payload={
                "provider_id": "meta_pages",
                "access_basis": "authorized_page",
                "coverage": "partial",
            },
        )


class InstagramHashtagConnector(SourceConnector):
    """Official, permission-gated Instagram hashtag discovery."""

    source_id = "instagram"
    label = "Instagram"
    group = "Approved API"
    capabilities = ConnectorCapabilities(
        True,
        interaction_fields=("like_count", "comment_count"),
    )

    @property
    def configured(self) -> bool:
        try:
            self._config()
        except ValueError:
            return False
        return True

    async def healthcheck(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(
                "not_configured",
                "Configure a pinned Meta Graph version, approved access token, and Instagram Professional user ID for hashtag discovery.",
            )
        return ConnectorStatus(
            "ready",
            "Official Instagram hashtag discovery is configured; coverage is best effort and permission-gated.",
        )

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        if not self.configured:
            return
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        terms = tuple(
            term
            for term in query.search_terms
            if self._is_hashtag_term(term)
        )
        if not terms:
            raise RuntimeError(
                "Instagram hashtag discovery requires a single-token hashtag such as #gamedev"
            )
        initial_cursor = query.resume_cursor(
            "query", default=None, legacy_key="cursor"
        )
        context = RunContext(
            run_id=f"instagram-keyword-{query.keyword_id}",
            keyword_id=query.keyword_id,
            source_id="instagram",
            provider_id="instagram_hashtag",
            operation="search",
            target={"kind": "hashtag"},
            terms=terms,
            filters={
                "access_basis": "hashtag",
                "coverage": "best_effort",
            },
            budgets=RunBudgets(
                max_items=query.max_items,
                max_requests=query.request_limit(
                    max(1, min(100, query.max_items + len(terms))), maximum=100
                ),
                deadline_seconds=query.deadline_limit(300),
            ),
        )
        adapter = InstagramHashtagSearchAdapter(
            self._provider(self._config()),
            self._pseudonymizer(),
        )
        cancellation = CancellationToken()
        await adapter.open(context, cancellation)
        try:
            async def fetch(cursor: object | None, limit: int):
                page = await adapter.fetch_page(context, cursor, limit)
                query.report_cursor("query", page.next_cursor)
                return page

            async for batch in bounded_pages(
                fetch,
                budgets=context.budgets,
                cancellation=cancellation,
                initial_cursor=initial_cursor,
                page_size=min(100, query.max_items),
            ):
                for record in batch.items:
                    yield self._raw_item(record)
        finally:
            await adapter.close()

    @staticmethod
    def _config() -> MetaGraphConfig:
        return MetaGraphConfig(
            settings.meta_graph_api_version,
            settings.meta_access_token or "",
            settings.instagram_professional_user_id,
        )

    @staticmethod
    def _provider(config: MetaGraphConfig) -> InstagramGraphHashtagProvider:
        account_digest = hashlib.sha256(
            config.instagram_user_id.encode("ascii")
        ).hexdigest()[:24]
        budget = InstagramHashtagBudget(
            settings.data_dir
            / "crawler-state"
            / "instagram-hashtags"
            / f"{account_digest}.json",
            config.instagram_user_id,
        )
        return InstagramGraphHashtagProvider(config, budget=budget)

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())

    @staticmethod
    def _is_hashtag_term(value: str) -> bool:
        normalized = str(value).strip().removeprefix("#")
        return bool(normalized and len(normalized) <= 100 and not any(character.isspace() for character in normalized) and "#" not in normalized)

    @staticmethod
    def _raw_item(record) -> RawContentItem:
        return RawContentItem(
            external_id=record.external_id,
            canonical_url=record.canonical_url,
            title=record.title,
            body_snippet=record.body,
            author=record.author_pseudonym,
            published_at=record.published_at,
            metrics=dict(record.metrics),
            raw_payload={
                "provider_id": "instagram_hashtag",
                "access_basis": "hashtag",
                "coverage": "best_effort",
            },
        )


class YouTubeConnector(SourceConnector):
    source_id = "youtube"
    label = "YouTube"
    group = "Official API"
    capabilities = ConnectorCapabilities(
        True,
        watchlist_filter=True,
        interaction_fields=("view_count", "like_count", "comment_count"),
    )

    @property
    def configured(self) -> bool:
        return bool(settings.youtube_api_key)

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.youtube_api_key:
            return ConnectorStatus(
                "not_configured",
                "Add YOUTUBE_API_KEY in backend/.env to enable official search.",
                "LOCAL_PREREQUISITE_MISSING",
            )
        return ConnectorStatus(
            "ready",
            "YouTube Data API key is configured; use deep health to verify access and quota.",
            probe="local",
        )

    async def deep_healthcheck(self) -> ConnectorStatus:
        if not settings.youtube_api_key:
            return await self.healthcheck()
        try:
            async with httpx.AsyncClient(
                base_url="https://www.googleapis.com/youtube/v3",
                timeout=30,
                follow_redirects=False,
            ) as client:
                await youtube_json(
                    client,
                    "/i18nLanguages",
                    params={"part": "snippet", "key": settings.youtube_api_key},
                )
        except CrawlerFailure as exc:
            state = {
                CrawlerErrorCode.AUTH_REQUIRED: "auth_required",
                CrawlerErrorCode.RATE_LIMITED: "rate_limited",
                CrawlerErrorCode.TRANSPORT_ERROR: "degraded",
            }.get(exc.code, "degraded")
            return ConnectorStatus(
                state,
                exc.safe_message,
                exc.code.value,
                "remote",
            )
        return ConnectorStatus(
            "ready",
            "YouTube Data API key and low-cost read probe succeeded.",
            probe="remote",
        )

    async def scan_comments(
        self,
        target_url: str,
        budgets: YouTubeCommentBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
    ) -> YouTubeCommentScan:
        if not settings.youtube_api_key:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "YouTube Data API key is not configured.",
            )
        async with httpx.AsyncClient(
            base_url="https://www.googleapis.com/youtube/v3",
            timeout=30,
            follow_redirects=False,
        ) as client:
            provider = YouTubeApiCommentProvider(
                client,
                api_key=settings.youtube_api_key,
                max_requests=budgets.max_requests,
            )
            adapter = YouTubeCommentsAdapter(provider, self._pseudonymizer())
            return await adapter.crawl(
                target_url,
                budgets,
                order=sort,
                cancellation=cancellation,
            )

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 100,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[CommentRecord]:
        if initial_cursor:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "YouTube comment scans do not accept a durable cursor.",
            )
        total = min(max(int(max_items), 1), 1_000)
        scan = await self.scan_comments(
            target_url,
            YouTubeCommentBudgets(
                max_root_comments=min(total, 100),
                max_children_per_root=min(total, 100),
                max_total_comments=total,
                max_requests=settings.youtube_comment_request_budget,
            ),
        )
        for record in scan.records:
            yield record

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del channel
        if operation == "search":
            return {
                "provider_contract": "youtube-data-v3-search-v2",
                "order": "date",
                "region": settings.youtube_region_code.strip().upper(),
                "language": settings.youtube_relevance_language.strip().casefold(),
                "time_window": "rolling_90d",
            }
        return {
            "provider_contract": "youtube-data-v3-uploads-v2",
            "order": "playlist_newest_first",
        }

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        if not settings.youtube_api_key:
            return
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        backlog_token = query.resume_cursor(
            "query", default=None, legacy_key="page_token"
        )
        page_token: str | None = None
        used_backlog = False
        known_ids = set(query.recent_ids)
        yielded = 0
        quota = YouTubeQuotaBudget(
            settings.youtube_search_request_budget,
            settings.youtube_general_request_budget,
            max_total_requests=query.request_limit(
                settings.youtube_search_request_budget
                + settings.youtube_general_request_budget,
                maximum=10_000,
            ),
        )
        published_after = (datetime.now(UTC) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
        async with httpx.AsyncClient(
            base_url="https://www.googleapis.com/youtube/v3",
            timeout=30,
            follow_redirects=False,
        ) as client:
            while yielded < query.max_items:
                if not quota.spend("/search"):
                    query.report_cursor("query", page_token)
                    break
                params: dict[str, Any] = {
                    "part": "snippet",
                    "type": "video",
                    "q": "|".join(query.search_terms),
                    "maxResults": min(50, query.max_items - yielded),
                    "order": "date",
                    "regionCode": settings.youtube_region_code,
                    "relevanceLanguage": settings.youtube_relevance_language,
                    "publishedAfter": published_after,
                    "key": settings.youtube_api_key,
                }
                if page_token:
                    params["pageToken"] = page_token
                try:
                    payload = await youtube_json(client, "/search", params=params)
                except CrawlerFailure as exc:
                    if page_token and is_invalid_page_token(exc):
                        query.report_cursor("query", None)
                        break
                    raise
                rows = payload.get("items", [])
                video_ids = [row.get("id", {}).get("videoId") for row in rows if row.get("id", {}).get("videoId")]
                if not video_ids:
                    query.report_cursor("query", None)
                    break
                page_has_unseen = any(video_id not in known_ids for video_id in video_ids)
                if not page_has_unseen:
                    if backlog_token and not used_backlog:
                        page_token = str(backlog_token)
                        used_backlog = True
                        continue
                    query.report_cursor("query", None)
                    break
                if not quota.spend("/videos"):
                    query.report_cursor("query", page_token)
                    break
                details_payload = await youtube_json(
                    client,
                    "/videos",
                    params={"part": "snippet,statistics", "id": ",".join(video_ids), "key": settings.youtube_api_key},
                )
                details = {row["id"]: row for row in details_payload.get("items", [])}
                for video_id in video_ids:
                    if video_id in known_ids or yielded >= query.max_items:
                        continue
                    row = details.get(video_id, {})
                    if not row:
                        continue
                    snippet = row.get("snippet", {})
                    statistics = row.get("statistics", {})
                    published = snippet.get("publishedAt")
                    published_at = datetime.fromisoformat(published) if published else None
                    yield RawContentItem(
                        external_id=video_id,
                        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
                        title=snippet.get("title", ""),
                        body_snippet=snippet.get("description", "")[:4000],
                        author=snippet.get("channelTitle", ""),
                        hashtags=[
                            str(tag) if str(tag).startswith("#") else f"#{tag}"
                            for tag in snippet.get("tags", [])
                            if str(tag).strip()
                        ],
                        locale=snippet.get("defaultLanguage"),
                        published_at=published_at,
                        metrics={
                            "view_count": int(statistics.get("viewCount", 0)),
                            "like_count": int(statistics.get("likeCount", 0)),
                            "comment_count": int(statistics.get("commentCount", 0)),
                        },
                        raw_payload={
                            "provider_id": "youtube_data_v3",
                            "video_id": video_id,
                            "channel_id": str(snippet.get("channelId") or ""),
                            "category_id": str(snippet.get("categoryId") or ""),
                            "live_broadcast_content": str(
                                snippet.get("liveBroadcastContent") or ""
                            ),
                        },
                    )
                    yielded += 1
                    known_ids.add(video_id)
                next_page_token = payload.get("nextPageToken")
                if yielded >= query.max_items:
                    query.report_cursor("query", next_page_token)
                    break
                if not next_page_token or next_page_token == page_token:
                    query.report_cursor("query", None)
                    break
                page_token = next_page_token
                await asyncio.sleep(0.2)


class JsonlCommandConnector(SourceConnector):
    """Runs an explicit local adapter; stdout must contain newline-delimited JSON items.

    This keeps MediaCrawler/browser automation out of the API process and never
    tries to create a login session or evade a platform challenge automatically.
    """

    def __init__(self, source_id: str, label: str, detail: str, capabilities: ConnectorCapabilities, platform: str):
        self.source_id = source_id
        self.label = label
        self.group = "MediaCrawler bridge"
        self.detail = detail
        self.capabilities = capabilities
        self.platform = platform

    async def healthcheck(self) -> ConnectorStatus:
        if settings.mediacrawler_command:
            return ConnectorStatus("ready", "Custom local JSONL bridge configured. Browser login remains user-visible.")
        project_root = Path(__file__).resolve().parents[3]
        adapter = project_root / "backend" / "scripts" / "mediacrawler_adapter.py"
        runtime = project_root / "vendor" / "mediacrawler" / ".venv" / "Scripts" / "python.exe"
        ready_marker = project_root / "data" / "mediacrawler-ready"
        if not adapter.exists() or not (project_root / "vendor" / "mediacrawler" / "main.py").exists():
            return ConnectorStatus("not_configured", "MediaCrawler submodule is missing. Run git submodule update --init --recursive.")
        if not runtime.exists() or not ready_marker.exists():
            return ConnectorStatus("setup_required", "Run .\\scripts\\setup-mediacrawler.ps1 once, then restart Content Bot.")
        coccoc_path = Path(settings.content_bot_coccoc_executable_path).expanduser()
        if not coccoc_path.is_file():
            return ConnectorStatus(
                "setup_required",
                f"Cốc Cốc was not found at {coccoc_path}. Set CONTENT_BOT_COCCOC_EXECUTABLE_PATH in backend/.env.",
            )
        return ConnectorStatus("ready", "Direct MediaCrawler adapter is installed. A visible browser opens when login is required.")

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        """Run the bridge, recovering once from a browser target that closed early."""
        yielded_any = False
        for attempt in range(2):
            try:
                async for item in self._search_once(query):
                    yielded_any = True
                    yield item
                return
            except RuntimeError as exc:
                error_text = str(exc)
                target_closed = "TargetClosedError" in error_text or "target page, context or browser has been closed" in error_text.casefold()
                if attempt or yielded_any or not target_closed:
                    raise
                if query.progress_callback:
                    try:
                        await query.progress_callback("retrying", "Cốc Cốc page was closed; reopening the browser session")
                    except Exception:
                        logger.debug("Progress callback failed while retrying browser session", exc_info=True)
                await asyncio.sleep(1)

    async def fetch_detail(self, target_url: str) -> RawContentItem:
        connector = self._tieba_target_connector()
        return await connector.fetch_detail(target_url)

    async def list_creator(
        self,
        target_url: str,
        *,
        max_items: int = 20,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[RawContentItem]:
        connector = self._tieba_target_connector()
        async for item in connector.list_creator(
            target_url,
            max_items=max_items,
            initial_cursor=initial_cursor,
        ):
            yield item

    async def scan_channel(
        self,
        channel: dict[str, Any],
        query: SearchQuery,
    ) -> AsyncIterator[RawContentItem]:
        connector = self._tieba_target_connector()
        async for item in connector.scan_channel(channel, query):
            yield item

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 20,
        initial_cursor: str | None = None,
    ):
        connector = self._tieba_target_connector()
        async for item in connector.list_comments(
            target_url,
            max_items=max_items,
            initial_cursor=initial_cursor,
        ):
            yield item

    async def scan_comments(
        self,
        target_url: str,
        budgets: RunBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
    ):
        connector = self._tieba_target_connector()
        return await connector.scan_comments(
            target_url,
            budgets,
            sort=sort,
            cancellation=cancellation,
        )

    def _tieba_target_connector(self):
        if self.source_id != "tieba":
            raise RuntimeError("Clean-room target operations are only wired for Tieba")
        from .cbce_connectors import CbceTiebaConnector

        return CbceTiebaConnector()

    async def _search_once(self, query: SearchQuery) -> AsyncIterator[RawContentItem]:
        project_root = Path(__file__).resolve().parents[3]
        substitutions = {
            "source": self.platform,
            "keywords": ",".join(query.include_terms),
            "max_items": str(query.max_items),
            "profile_dir": str(settings.mediacrawler_profile_dir),
        }
        if settings.mediacrawler_command:
            command = [part.format(**substitutions) for part in shlex.split(settings.mediacrawler_command, posix=False)]
        else:
            command = [sys.executable, str(project_root / "backend" / "scripts" / "mediacrawler_adapter.py")]
        command.extend(["--source", self.platform, "--keywords", ",".join(query.include_terms), "--max-items", str(query.max_items), "--profile-dir", str(settings.mediacrawler_profile_dir)])
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_tail = bytearray()
        stderr_pending = ""

        async def report_progress_lines(lines: list[str]) -> None:
            if not query.progress_callback:
                return
            for line in lines:
                progress = login_progress_from_stderr(line)
                if not progress:
                    continue
                try:
                    await query.progress_callback(*progress)
                except Exception:
                    # Progress reporting must never stop the crawler process.
                    logger.debug("Progress callback failed", exc_info=True)
                    continue

        async def drain_stderr() -> None:
            nonlocal stderr_pending
            while chunk := await process.stderr.read(4096):
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 32_000:
                    del stderr_tail[:-32_000]
                stderr_pending += chunk.decode("utf-8", errors="replace")
                complete_lines = stderr_pending.split("\n")
                stderr_pending = complete_lines.pop()
                await report_progress_lines(complete_lines)
            if stderr_pending:
                await report_progress_lines([stderr_pending])

        stderr_task = asyncio.create_task(drain_stderr())
        yielded = 0
        try:
            async with asyncio.timeout(
                query.deadline_limit(settings.mediacrawler_timeout_seconds)
            ):
                async for line in process.stdout:
                    if yielded >= query.max_items:
                        await self._stop_process_tree(process)
                        break
                    try:
                        payload = json.loads(line.decode("utf-8"))
                        published = payload.get("published_at")
                        yield RawContentItem(
                            external_id=str(payload["external_id"]),
                            canonical_url=str(payload["canonical_url"]),
                            title=str(payload.get("title", "")),
                            body_snippet=str(payload.get("body_snippet", ""))[:4000],
                            author=str(payload.get("author", "")),
                            hashtags=[str(tag) for tag in payload.get("hashtags", [])],
                            locale=payload.get("locale"),
                            published_at=datetime.fromisoformat(published) if published else None,
                            metrics={key: int(value or 0) for key, value in payload.get("metrics", {}).items()},
                            raw_payload=payload,
                        )
                        yielded += 1
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                await process.wait()
        except TimeoutError as exc:
            raise RuntimeError("MediaCrawler timed out while waiting for login or source results.") from exc
        finally:
            if process.returncode is None:
                await self._stop_process_tree(process)
            await stderr_task
        if process.returncode not in (0, None):
            detail = stderr_tail.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(detail or f"Bridge exited {process.returncode}")

    @staticmethod
    async def _stop_process_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


class FeedConnector(SourceConnector):
    """Small allowlisted RSS/Atom connector for public game sites and Steam feeds."""

    def __init__(self, source_id: str, label: str, detail: str, url_templates: str):
        self.source_id = source_id
        self.label = label
        self.group = "Public web"
        self.detail = detail
        self.configuration_error: str | None = None
        try:
            self.feed_templates = parse_feed_templates(url_templates)
        except ValueError as exc:
            self.feed_templates = ()
            self.configuration_error = str(exc)
        self.url_templates = [template.value for template in self.feed_templates]
        self.capabilities = ConnectorCapabilities(True, watchlist_filter=True)

    async def healthcheck(self) -> ConnectorStatus:
        if self.configuration_error:
            return ConnectorStatus(
                "not_configured",
                self.configuration_error,
                "INVALID_FEED_CONFIGURATION",
            )
        if not self.feed_templates:
            return ConnectorStatus("not_configured", self.detail)
        return ConnectorStatus(
            "ready",
            f"{len(self.feed_templates)} validated public RSS/Atom feed template(s) configured.",
        )

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del operation, channel
        return {
            "provider_contract": "rss-atom-v2",
            "feed_templates": [template.digest for template in self.feed_templates],
        }

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        if not self.feed_templates:
            return
        seen: set[str] = set()
        successful_documents = 0
        failures: list[CrawlerFailure] = []
        allowed_hosts = frozenset(template.host for template in self.feed_templates)
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "ContentBot/0.1 (local research tool)"},
        ) as client:
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            for search_term, term_limit in zip(query.search_terms, term_limits, strict=True):
                term_yielded = 0
                if term_limit == 0:
                    continue
                for template in self.feed_templates:
                    validators = query.resume_cursor(
                        "feed",
                        feed=template.digest,
                        term=search_term,
                        default={},
                    )
                    try:
                        document = await fetch_feed_document(
                            client,
                            template.render(search_term),
                            allowed_hosts=allowed_hosts,
                            validators=(
                                validators if isinstance(validators, dict) else {}
                            ),
                        )
                        if document.not_modified:
                            successful_documents += 1
                            continue
                        root = ElementTree.fromstring(document.content)
                        nodes = self._feed_nodes(root)
                    except (CrawlerFailure, ElementTree.ParseError, ValueError) as exc:
                        failure = (
                            exc
                            if isinstance(exc, CrawlerFailure)
                            else CrawlerFailure(
                                CrawlerErrorCode.PARSE_CHANGED,
                                "Feed document is not valid RSS/Atom XML.",
                            )
                        )
                        failures.append(failure)
                        logger.warning(
                            "Skipping invalid feed document: feed=%s code=%s",
                            template.digest[:12],
                            failure.code.value,
                        )
                        if query.warning_callback:
                            await query.warning_callback(
                                failure.code.value,
                                failure.safe_message,
                            )
                        continue
                    successful_documents += 1
                    remaining_before = term_limit - term_yielded
                    for node in nodes:
                        if term_yielded >= term_limit:
                            break
                        title = self._text(node, "title")
                        link = self._entry_link(node)
                        canonical_url = safe_entry_url(link, base_url=document.url)
                        external_id = (
                            node.findtext("guid")
                            or node.findtext("{http://www.w3.org/2005/Atom}id")
                            or canonical_url
                        )
                        if not external_id or external_id in seen:
                            continue
                        if canonical_url is None:
                            canonical_url = safe_entry_url(
                                str(external_id),
                                base_url=document.url,
                            )
                        if canonical_url is None:
                            continue
                        seen.add(external_id)
                        published = self._text(node, "pubDate") or self._text(node, "published") or self._text(node, "updated")
                        published_at = parse_feed_datetime(published)
                        publisher_name, publisher_url = self._publisher(node, document.url)
                        yield RawContentItem(
                            external_id=str(external_id),
                            canonical_url=canonical_url,
                            title=plain_text(title, 180),
                            body_snippet=plain_text(
                                self._text(node, "description")
                                or self._text(node, "summary")
                                or self._text(node, "content")
                            ),
                            author=self._author(node),
                            published_at=published_at,
                            raw_payload={
                                "provider_id": "rss_atom",
                                "feed_id": template.digest,
                                "feed_host": template.host,
                                "publisher_name": publisher_name,
                                "publisher_url": publisher_url,
                                "media": self._media_metadata(node, document.url),
                                "search_term": search_term,
                            },
                        )
                        term_yielded += 1
                    if len(nodes) <= remaining_before:
                        next_validators = {
                            key: value
                            for key, value in {
                                "etag": document.etag,
                                "last_modified": document.last_modified,
                            }.items()
                            if value
                        }
                        if next_validators:
                            query.report_cursor(
                                "feed",
                                next_validators,
                                feed=template.digest,
                                term=search_term,
                            )
                    if term_yielded >= term_limit:
                        break
        if successful_documents == 0 and failures:
            raise failures[0]

    @staticmethod
    def _text(node: ElementTree.Element, tag: str) -> str:
        direct = node.findtext(tag)
        if direct:
            return direct.strip()
        namespaced = node.findtext(f"{{http://www.w3.org/2005/Atom}}{tag}")
        return namespaced.strip() if namespaced else ""

    @staticmethod
    def _feed_nodes(root: ElementTree.Element) -> list[ElementTree.Element]:
        local_name = root.tag.rsplit("}", 1)[-1].casefold()
        if local_name not in {"rss", "feed", "rdf"}:
            raise ValueError("XML root is not RSS/Atom")
        return root.findall(".//item") + root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )

    @staticmethod
    def _entry_link(node: ElementTree.Element) -> str:
        direct = node.findtext("link")
        if direct:
            return direct.strip()
        links = node.findall("{http://www.w3.org/2005/Atom}link")
        preferred = next(
            (
                entry.get("href", "")
                for entry in links
                if entry.get("href") and entry.get("rel", "alternate") == "alternate"
            ),
            "",
        )
        return preferred or next(
            (entry.get("href", "") for entry in links if entry.get("href")),
            "",
        )

    @staticmethod
    def _author(node: ElementTree.Element) -> str:
        values = (
            node.findtext("author"),
            node.findtext("{http://purl.org/dc/elements/1.1/}creator"),
            node.findtext("{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name"),
        )
        return plain_text(next((value for value in values if value), ""), 180)

    @staticmethod
    def _publisher(
        node: ElementTree.Element,
        base_url: str,
    ) -> tuple[str, str | None]:
        source = node.find("source")
        if source is None:
            return "", None
        name = plain_text(source.text or "", 180)
        url = safe_entry_url(str(source.get("url") or ""), base_url=base_url)
        return name, url

    @staticmethod
    def _media_metadata(
        node: ElementTree.Element,
        base_url: str,
    ) -> list[dict[str, Any]]:
        candidates: list[tuple[str, str, str, str]] = []
        for enclosure in node.findall("enclosure"):
            candidates.append(
                (
                    "enclosure",
                    str(enclosure.get("url") or ""),
                    str(enclosure.get("type") or ""),
                    str(enclosure.get("length") or ""),
                )
            )
        for link in node.findall("{http://www.w3.org/2005/Atom}link"):
            if str(link.get("rel") or "") == "enclosure":
                candidates.append(
                    (
                        "enclosure",
                        str(link.get("href") or ""),
                        str(link.get("type") or ""),
                        str(link.get("length") or ""),
                    )
                )
        for thumbnail in node.findall("{http://search.yahoo.com/mrss/}thumbnail"):
            candidates.append(
                (
                    "thumbnail",
                    str(thumbnail.get("url") or ""),
                    "image/*",
                    "",
                )
            )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for kind, raw_url, mime_type, raw_length in candidates:
            url = safe_entry_url(raw_url, base_url=base_url)
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                size_bytes = int(raw_length) if raw_length else None
            except ValueError:
                size_bytes = None
            result.append(
                {
                    "kind": kind,
                    "url": url,
                    "mime_type": mime_type[:200] or None,
                    "size_bytes": (
                        size_bytes if size_bytes is not None and size_bytes >= 0 else None
                    ),
                }
            )
            if len(result) >= 10:
                break
        return result


class SteamReviewsConnector(SourceConnector):
    source_id = "steam"
    label = "Steam reviews"
    group = "Public game community"
    capabilities = ConnectorCapabilities(
        True,
        watchlist_filter=True,
        interaction_fields=("like_count", "comment_count"),
    )

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "Public Steam game search and recent user reviews; no API key required.")

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del operation, channel
        return {
            "provider_contract": "steam-public-reviews-v2",
            "review_filter": settings.steam_review_filter.strip().casefold(),
            "language": settings.steam_review_language.strip().casefold(),
            "purchase_type": settings.steam_purchase_type.strip().casefold(),
            "max_discovered_apps": max(
                1,
                min(settings.steam_max_discovered_apps, 10),
            ),
        }

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        budget = SteamRequestBudget(
            max(1, min(settings.steam_discovery_request_budget, 20)),
            max(1, min(settings.steam_review_request_budget, 100)),
            total_limit=query.request_limit(
                settings.steam_discovery_request_budget
                + settings.steam_review_request_budget,
                maximum=120,
            ),
        )
        max_apps = max(1, min(settings.steam_max_discovered_apps, 10))
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "ContentBot/0.1 (local non-commercial game research)"},
        ) as client:
            games: list[dict[str, Any]] = []
            seen_apps: set[str] = set()
            game_limits = allocate_limits(max_apps, len(query.search_terms))
            for search_term, game_limit in zip(query.search_terms, game_limits, strict=True):
                if game_limit == 0:
                    continue
                if not budget.spend_discovery():
                    if query.warning_callback:
                        await query.warning_callback(
                            CrawlerErrorCode.BUDGET_EXHAUSTED.value,
                            "Steam app-discovery request budget was exhausted.",
                        )
                    break
                search_response = await get_with_retries(
                    client,
                    "https://store.steampowered.com/api/storesearch/",
                    params={"term": search_term, "l": "english", "cc": "VN"},
                )
                selected, ambiguous = select_discovered_apps(
                    search_term,
                    list(search_response.json().get("items") or []),
                    limit=game_limit,
                )
                if ambiguous:
                    if query.warning_callback:
                        await query.warning_callback(
                            "AMBIGUOUS_APP_MATCH",
                            f"Steam app discovery was ambiguous for {search_term!r}; save an exact /app/<id> URL instead.",
                        )
                    continue
                for game in selected:
                    app_id = str(game.get("id", ""))
                    if app_id and app_id not in seen_apps:
                        seen_apps.add(app_id)
                        games.append(game)
            review_limits = allocate_limits(query.max_items, len(games))
            for game, review_limit in zip(games, review_limits, strict=True):
                game_yielded = 0
                if review_limit == 0:
                    continue
                app_id = str(game.get("id", ""))
                game_name = str(game.get("name", query.name))
                if not app_id:
                    continue
                scope = {
                    "app_id": app_id,
                    "filter": settings.steam_review_filter,
                    "language": settings.steam_review_language,
                    "purchase_type": settings.steam_purchase_type,
                }
                backlog_cursor = str(
                    query.resume_cursor(
                        "app", default="", **scope
                    )
                    or ""
                )
                cursor = "*"
                used_backlog = False
                known_ids = set(query.recent_ids)
                while game_yielded < review_limit:
                    if not budget.spend_review():
                        query.report_cursor(
                            "app",
                            backlog_cursor or (cursor if cursor != "*" else None),
                            **scope,
                        )
                        if query.warning_callback:
                            await query.warning_callback(
                                CrawlerErrorCode.BUDGET_EXHAUSTED.value,
                                "Steam review request budget was exhausted.",
                            )
                        return
                    response = await get_with_retries(
                        client,
                        f"https://store.steampowered.com/appreviews/{app_id}",
                        params={
                            "json": 1,
                            "filter": settings.steam_review_filter,
                            "language": settings.steam_review_language,
                            "purchase_type": settings.steam_purchase_type,
                            "num_per_page": min(100, review_limit - game_yielded),
                            "cursor": cursor,
                        },
                    )
                    payload = response.json()
                    reviews = payload.get("reviews", [])
                    if payload.get("success") != 1 or not reviews:
                        query.report_cursor("app", None, **scope)
                        break
                    page_has_unseen = any(
                        steam_review_external_id(app_id, review) not in known_ids
                        for review in reviews
                        if review.get("recommendationid")
                    )
                    if not page_has_unseen:
                        if backlog_cursor and not used_backlog:
                            cursor = backlog_cursor
                            used_backlog = True
                            continue
                        query.report_cursor("app", None, **scope)
                        break
                    for review in reviews:
                        if game_yielded >= review_limit:
                            break
                        review_id = str(review.get("recommendationid", ""))
                        external_id = f"{app_id}:{review_id}"
                        if not review_id or external_id in known_ids:
                            continue
                        yield self.raw_review(review, app_id=app_id, game_name=game_name)
                        known_ids.add(external_id)
                        game_yielded += 1
                    next_cursor = str(payload.get("cursor") or "")
                    if game_yielded >= review_limit:
                        query.report_cursor("app", next_cursor or None, **scope)
                        break
                    if not next_cursor or next_cursor == cursor:
                        query.report_cursor("app", None, **scope)
                        break
                    cursor = next_cursor
                    await asyncio.sleep(0.2)

    @staticmethod
    def raw_review(
        review: dict[str, Any],
        *,
        app_id: str,
        game_name: str,
    ) -> RawContentItem:
        timestamp = int(review.get("timestamp_created", 0) or 0)
        voted_up = bool(review.get("voted_up"))
        return RawContentItem(
            external_id=steam_review_external_id(app_id, review),
            canonical_url=f"https://steamcommunity.com/app/{app_id}/reviews/",
            title=f"{'Recommended' if voted_up else 'Not recommended'} — {game_name}",
            body_snippet=str(review.get("review") or "")[:4_000],
            author="Steam reviewer",
            locale=str(review.get("language") or "") or None,
            published_at=(
                datetime.fromtimestamp(timestamp, tz=UTC) if timestamp else None
            ),
            metrics={
                "like_count": int(review.get("votes_up", 0) or 0),
                "comment_count": int(review.get("comment_count", 0) or 0),
            },
            raw_payload=steam_review_payload(
                review,
                app_id=app_id,
                game_name=game_name,
            ),
        )


class BlueskyConnector(SourceConnector):
    source_id = "bluesky"
    label = "Bluesky"
    group = "Public social API"
    capabilities = ConnectorCapabilities(
        True,
        watchlist_filter=True,
        interaction_fields=("like_count", "comment_count", "share_count"),
    )

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "Public Bluesky keyword search; no account or API key required.")

    async def scan_comments(
        self,
        target_url: str,
        budgets: BlueskyCommentBudgets,
        *,
        sort: str = "provider",
        cancellation: CancellationToken | None = None,
    ) -> BlueskyCommentScan:
        async with httpx.AsyncClient(
            base_url="https://public.api.bsky.app",
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "ContentBot/0.1 (public AppView reader)"},
        ) as client:
            provider = BlueskyApiCommentProvider(
                client,
                max_requests=budgets.max_requests,
            )
            adapter = BlueskyCommentsAdapter(provider, self._pseudonymizer())
            return await adapter.crawl(
                target_url,
                budgets,
                sort=sort,
                cancellation=cancellation,
            )

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 100,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[CommentRecord]:
        if initial_cursor:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bluesky post threads do not expose a durable reply cursor.",
            )
        total = min(max(int(max_items), 1), 1_000)
        scan = await self.scan_comments(
            target_url,
            BlueskyCommentBudgets(
                max_root_comments=min(total, 500),
                max_children_per_root=min(total, 500),
                max_total_comments=total,
                max_requests=5,
                max_depth=8,
            ),
        )
        for record in scan.records:
            yield record

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "provider_contract": "bluesky-appview-v2",
            "sort": "latest" if operation == "search" else "author_feed",
            **(
                {
                    "include_replies": bool(channel.get("include_replies", False)),
                    "include_reposts": bool(channel.get("include_reposts", False)),
                }
                if channel
                else {}
            ),
        }

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        seen_posts: set[str] = set()
        request_budget = query.request_limit(
            settings.bluesky_request_budget, maximum=100
        )
        requests = 0
        async with httpx.AsyncClient(
            base_url="https://public.api.bsky.app",
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "ContentBot/0.1 (public AppView reader)"},
        ) as client:
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            for term_index, (search_term, term_limit) in enumerate(
                zip(query.search_terms, term_limits, strict=True)
            ):
                term_yielded = 0
                if term_limit == 0:
                    continue
                backlog_cursor = str(
                    query.resume_cursor(
                        "term",
                        term=search_term,
                        default="",
                        legacy_key="cursor" if term_index == 0 else None,
                    )
                    or ""
                )
                cursor = ""
                used_backlog = False
                known_ids = set(query.recent_ids)
                while term_yielded < term_limit:
                    if requests >= request_budget:
                        query.report_cursor(
                            "term",
                            backlog_cursor or cursor or None,
                            term=search_term,
                        )
                        if query.warning_callback:
                            await query.warning_callback(
                                CrawlerErrorCode.BUDGET_EXHAUSTED.value,
                                "Bluesky AppView request budget was exhausted.",
                            )
                        return
                    params: dict[str, Any] = {
                        "q": search_term,
                        "sort": "latest",
                        "limit": min(50, term_limit - term_yielded),
                    }
                    if cursor:
                        params["cursor"] = cursor
                    requests += 1
                    try:
                        payload = await bluesky_json(
                            client,
                            "/xrpc/app.bsky.feed.searchPosts",
                            params=params,
                        )
                    except CrawlerFailure as exc:
                        if cursor and bluesky_invalid_cursor(exc):
                            query.report_cursor("term", None, term=search_term)
                            break
                        raise
                    posts = payload.get("posts") or []
                    if not isinstance(posts, list):
                        raise CrawlerFailure(
                            CrawlerErrorCode.PARSE_CHANGED,
                            "Bluesky search response has an invalid posts collection.",
                        )
                    if not posts:
                        query.report_cursor("term", None, term=search_term)
                        break
                    identities = [
                        (post, bluesky_post_identity(post))
                        for post in posts
                        if isinstance(post, dict)
                    ]
                    valid_identities = [
                        (post, identity)
                        for post, identity in identities
                        if identity is not None
                    ]
                    if not valid_identities:
                        raise CrawlerFailure(
                            CrawlerErrorCode.PARSE_CHANGED,
                            "Bluesky posts no longer contain valid AT URI identities.",
                        )
                    page_has_unseen = any(
                        stable_external_id("bsky", identity[0]) not in known_ids
                        and stable_external_id("bsky", identity[0]) not in seen_posts
                        for _post, identity in valid_identities
                    )
                    if not page_has_unseen:
                        if backlog_cursor and not used_backlog:
                            cursor = backlog_cursor
                            used_backlog = True
                            continue
                        query.report_cursor("term", None, term=search_term)
                        break
                    for post, identity in valid_identities:
                        if term_yielded >= term_limit:
                            break
                        uri, record_key = identity
                        external_id = stable_external_id("bsky", uri)
                        if external_id in seen_posts or external_id in known_ids:
                            continue
                        item = self.raw_post(
                            post,
                            record_key=record_key,
                            external_id=external_id,
                            discovery={"search_term": search_term},
                        )
                        if item is None:
                            continue
                        seen_posts.add(external_id)
                        known_ids.add(external_id)
                        yield item
                        term_yielded += 1
                    next_cursor = str(payload.get("cursor") or "")
                    if term_yielded >= term_limit:
                        query.report_cursor(
                            "term", next_cursor or None, term=search_term
                        )
                        break
                    if not next_cursor or next_cursor == cursor:
                        query.report_cursor("term", None, term=search_term)
                        break
                    cursor = next_cursor
                    await asyncio.sleep(0.5)

    @staticmethod
    def raw_post(
        post: dict[str, Any],
        *,
        record_key: str,
        external_id: str,
        discovery: dict[str, Any],
    ) -> RawContentItem | None:
        record = post.get("record") or {}
        author = post.get("author") or {}
        if not isinstance(record, dict) or not isinstance(author, dict):
            return None
        handle = str(author.get("handle") or "").strip().casefold()
        if not handle or "/" in handle or len(handle) > 253:
            return None
        body = str(record.get("text") or "")[:4_000]
        tags = bluesky_tags(record)
        return RawContentItem(
            external_id=external_id,
            canonical_url=f"https://bsky.app/profile/{handle}/post/{record_key}",
            title=next(
                (line.strip() for line in body.splitlines() if line.strip()),
                body,
            )[:180],
            body_snippet=body,
            author=handle,
            hashtags=tags,
            locale=next(iter(record.get("langs") or []), None),
            published_at=bluesky_datetime(
                record.get("createdAt") or post.get("indexedAt")
            ),
            metrics={
                "like_count": int(post.get("likeCount", 0) or 0),
                "comment_count": int(post.get("replyCount", 0) or 0),
                "share_count": int(post.get("repostCount", 0) or 0)
                + int(post.get("quoteCount", 0) or 0),
            },
            raw_payload=minimized_post_payload(
                post,
                tags=tags,
                discovery=discovery,
            ),
        )


class RedditConnector(SourceConnector):
    source_id = "reddit"
    label = "Reddit"
    group = "Public community"
    capabilities = ConnectorCapabilities(
        True,
        interaction_fields=("like_count", "comment_count"),
    )

    @property
    def configured(self) -> bool:
        return bool(settings.reddit_client_id and settings.reddit_client_secret)

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            return ConnectorStatus(
                "not_configured",
                "Add REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET for official Reddit OAuth search.",
            )
        return ConnectorStatus("ready", "Official Reddit OAuth keyword search is configured.")

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        """Search public Reddit submissions through application-only OAuth."""
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            return
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        yielded = 0
        seen_posts: set[str] = set()
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": settings.reddit_user_agent},
        ) as client:
            access_token = await reddit_token_cache.get(
                client,
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
            )
            client.headers["Authorization"] = f"Bearer {access_token}"
            pseudonymizer = self._pseudonymizer()
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            request_budget = query.request_limit(100, maximum=100)
            requests = 0
            for term_index, (search_term, term_limit) in enumerate(
                zip(query.search_terms, term_limits, strict=True)
            ):
                term_yielded = 0
                if term_limit == 0:
                    continue
                after = str(
                    query.resume_cursor(
                        "term",
                        term=search_term,
                        default="",
                        legacy_key="after" if term_index == 0 else None,
                    )
                    or ""
                )
                while term_yielded < term_limit:
                    if requests >= request_budget:
                        query.report_cursor(
                            "term", after or None, term=search_term
                        )
                        if query.warning_callback:
                            await query.warning_callback(
                                CrawlerErrorCode.BUDGET_EXHAUSTED.value,
                                "Reddit search request budget was exhausted.",
                            )
                        return
                    params: dict[str, Any] = {
                        "q": search_term,
                        "sort": "new",
                        "limit": min(100, term_limit - term_yielded),
                        "raw_json": 1,
                        "type": "link",
                    }
                    if after:
                        params["after"] = after
                    requests += 1
                    response = await get_with_retries(
                        client, "https://oauth.reddit.com/search", params=params
                    )
                    listing = response.json().get("data") or {}
                    children = listing.get("children") or []
                    if not children:
                        query.report_cursor("term", None, term=search_term)
                        break
                    for child in children:
                        post = child.get("data") or {}
                        post_id = str(post.get("id") or "")
                        permalink = str(post.get("permalink") or "")
                        if not post_id or post_id in seen_posts or not permalink:
                            continue
                        seen_posts.add(post_id)
                        created_at = self._parse_timestamp(post.get("created_utc"))
                        flair = str(post.get("link_flair_text") or "").strip()
                        yield RawContentItem(
                            external_id=post_id,
                            canonical_url=f"https://www.reddit.com{permalink}",
                            title=str(post.get("title") or "")[:180],
                            body_snippet=str(post.get("selftext") or "")[:4000],
                            author=pseudonymizer.pseudonym(
                                "reddit", str(post.get("author") or "")
                            ),
                            hashtags=[f"#{flair}"] if flair else [],
                            published_at=created_at,
                            metrics={
                                "like_count": int(post.get("score", 0) or 0),
                                "comment_count": int(post.get("num_comments", 0) or 0),
                            },
                            raw_payload={
                                "post_id": post_id,
                                "subreddit": str(post.get("subreddit") or ""),
                                "score": int(post.get("score", 0) or 0),
                                "search_term": search_term,
                                "is_self": bool(post.get("is_self")),
                                "outbound_url": str(post.get("url") or ""),
                                "flair": flair,
                            },
                        )
                        yielded += 1
                        term_yielded += 1
                    next_after = str(listing.get("after") or "")
                    query.report_cursor(
                        "term", next_after or None, term=search_term
                    )
                    if not next_after or next_after == after:
                        break
                    after = next_after
                    await asyncio.sleep(0.5)

    async def scan_comments(
        self,
        target_url: str,
        budgets: RedditCommentBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
    ) -> RedditCommentScan:
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Reddit OAuth credentials are not configured.",
            )
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": settings.reddit_user_agent},
        ) as client:
            access_token = await reddit_token_cache.get(
                client,
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
            )
            client.headers["Authorization"] = f"Bearer {access_token}"
            provider = RedditApiCommentProvider(
                client, max_requests=budgets.max_requests
            )
            adapter = RedditCommentsAdapter(provider, self._pseudonymizer())
            return await adapter.crawl(
                target_url,
                budgets,
                sort=sort,
                cancellation=cancellation,
            )

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 100,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[CommentRecord]:
        if initial_cursor:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Reddit comment scans do not accept a durable cursor.",
            )
        total = min(max(int(max_items), 1), 1_000)
        scan = await self.scan_comments(
            target_url,
            RedditCommentBudgets(
                max_root_comments=min(total, 100),
                max_children_per_root=min(total, 100),
                max_total_comments=total,
                max_requests=settings.reddit_comment_request_budget,
            ),
        )
        for record in scan.records:
            yield record

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        try:
            return datetime.fromtimestamp(float(value), tz=UTC) if value is not None else None
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())


class MastodonConnector(SourceConnector):
    source_id = "mastodon"
    label = "Mastodon"
    group = "Public social API"
    capabilities = ConnectorCapabilities(
        True,
        watchlist_filter=True,
        interaction_fields=("like_count", "comment_count", "share_count"),
    )

    async def healthcheck(self) -> ConnectorStatus:
        try:
            instances = mastodon_instances(settings.mastodon_instances)
        except ValueError:
            return ConnectorStatus(
                "not_configured",
                "MASTODON_INSTANCES contains no valid public HTTPS instance origins.",
                reason_code="INVALID_INSTANCE_CONFIG",
            )
        return ConnectorStatus(
            "ready",
            f"{len(instances)} public Mastodon instance(s) configured; use deep health for a remote probe.",
            probe="local",
        )

    async def deep_healthcheck(self) -> ConnectorStatus:
        try:
            instances = mastodon_instances(settings.mastodon_instances)
        except ValueError:
            return await self.healthcheck()
        healthy: list[str] = []
        failed: list[str] = []
        budget = MastodonRequestBudget(len(instances))
        for instance in instances:
            try:
                async with httpx.AsyncClient(
                    base_url=f"https://{instance}",
                    timeout=10,
                    follow_redirects=False,
                    headers={"User-Agent": "ContentBot/0.1 (Mastodon public API)"},
                ) as client:
                    payload, _response = await mastodon_json(
                        client,
                        "/api/v2/instance",
                        budget=budget,
                        attempts=1,
                    )
                if not isinstance(payload, dict):
                    raise CrawlerFailure(
                        CrawlerErrorCode.PARSE_CHANGED,
                        "Mastodon instance metadata was invalid.",
                    )
                healthy.append(instance)
            except (CrawlerFailure, httpx.HTTPError):
                failed.append(instance)
        if not healthy:
            return ConnectorStatus(
                "degraded",
                f"All {len(instances)} configured Mastodon instances failed the explicit remote probe.",
                reason_code="ALL_INSTANCES_UNAVAILABLE",
                probe="deep",
            )
        if failed:
            return ConnectorStatus(
                "degraded",
                f"{len(healthy)}/{len(instances)} configured Mastodon instances passed the remote probe.",
                reason_code="PARTIAL_INSTANCE_OUTAGE",
                probe="deep",
            )
        return ConnectorStatus(
            "ready",
            f"All {len(instances)} configured Mastodon instances passed the remote probe.",
            probe="deep",
        )

    async def scan_comments(
        self,
        target_url: str,
        budgets: MastodonCommentBudgets,
        *,
        sort: str = "provider",
        cancellation: CancellationToken | None = None,
    ) -> MastodonCommentScan:
        instances = mastodon_instances(settings.mastodon_instances)
        target = parse_mastodon_status_target(
            target_url, allowed_instances=instances
        )
        async with httpx.AsyncClient(
            base_url=f"https://{target.instance}",
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "ContentBot/0.1 (Mastodon public API)"},
        ) as client:
            provider = MastodonApiCommentProvider(
                client,
                max_requests=budgets.max_requests,
            )
            adapter = MastodonCommentsAdapter(
                provider,
                self._pseudonymizer(),
                fetching_instance=target.instance,
            )
            return await adapter.crawl(
                target_url,
                budgets,
                allowed_instances=instances,
                sort=sort,
                cancellation=cancellation,
            )

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 100,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[CommentRecord]:
        if initial_cursor:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Mastodon status contexts do not expose a durable reply cursor.",
            )
        total = min(max(int(max_items), 1), 1_000)
        scan = await self.scan_comments(
            target_url,
            MastodonCommentBudgets(
                max_root_comments=min(total, 500),
                max_children_per_root=min(total, 500),
                max_total_comments=total,
                max_requests=5,
                max_depth=8,
            ),
        )
        for record in scan.records:
            yield record

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "operation": operation,
            "provider_contract": "mastodon-public-v2",
            "discovery_mode": "instance_hashtag_timeline",
            "instances": list(mastodon_instances(settings.mastodon_instances)),
        }
        if channel is not None:
            fields.update(
                {
                    "normalized_target": str(
                        channel.get("normalized_url") or channel.get("url") or ""
                    ),
                    "include_replies": bool(channel.get("include_replies")),
                    "include_reposts": bool(channel.get("include_reposts")),
                }
            )
        return fields

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        instances = mastodon_instances(settings.mastodon_instances)
        budget = MastodonRequestBudget(
            query.request_limit(settings.mastodon_request_budget, maximum=500)
        )
        seen_ids: set[str] = set(query.recent_ids)
        yielded = 0
        term_limits = allocate_limits(query.max_items, len(query.search_terms))
        successful_requests = 0
        failures: list[CrawlerFailure] = []

        for search_term, term_limit in zip(query.search_terms, term_limits, strict=True):
            hashtag = mastodon_hashtag(search_term)
            if term_limit <= 0 or not hashtag:
                continue
            term_yielded = 0
            instance_limits = allocate_limits(term_limit, len(instances))
            for instance, instance_limit in zip(instances, instance_limits, strict=True):
                if term_yielded >= term_limit:
                    break
                if instance_limit <= 0:
                    continue
                instance_yielded = 0
                backlog = str(
                    query.resume_cursor(
                        "hashtag",
                        instance=instance,
                        hashtag=hashtag,
                        default="",
                    )
                    or ""
                )
                max_id = ""
                frontier = True
                seen_cursors: set[str] = set()
                async with httpx.AsyncClient(
                    base_url=f"https://{instance}",
                    timeout=30,
                    follow_redirects=False,
                    headers={"User-Agent": "ContentBot/0.1 (Mastodon public API)"},
                ) as client:
                    while term_yielded < term_limit and instance_yielded < instance_limit:
                        params: dict[str, Any] = {
                            "limit": min(40, instance_limit - instance_yielded),
                        }
                        if max_id:
                            params["max_id"] = max_id
                        try:
                            payload, response = await mastodon_json(
                                client,
                                f"/api/v1/timelines/tag/{hashtag}",
                                params=params,
                                budget=budget,
                            )
                        except CrawlerFailure as exc:
                            failures.append(exc)
                            if query.warning_callback:
                                await query.warning_callback(
                                    "MASTODON_INSTANCE_FAILED",
                                    f"Mastodon instance {instance} could not be scanned; other configured instances will continue.",
                                )
                            break
                        successful_requests += 1
                        if not isinstance(payload, list):
                            failures.append(
                                CrawlerFailure(
                                    CrawlerErrorCode.PARSE_CHANGED,
                                    "Mastodon hashtag timeline response was invalid.",
                                )
                            )
                            break
                        posts = [item for item in payload if isinstance(item, dict)]
                        if not posts:
                            query.report_cursor(
                                "hashtag", None, instance=instance, hashtag=hashtag
                            )
                            break
                        normalized = [
                            normalize_mastodon_status(
                                post,
                                fetching_instance=instance,
                                discovery={
                                    "hashtag": hashtag,
                                    "search_term": search_term,
                                },
                            )
                            for post in posts
                        ]
                        valid = [item for item in normalized if item is not None]
                        overlap = any(item["external_id"] in seen_ids for item in valid)
                        next_cursor = mastodon_next_max_id(response, posts)

                        if frontier and overlap and backlog:
                            max_id = backlog
                            frontier = False
                            continue
                        if frontier and overlap:
                            query.report_cursor(
                                "hashtag", None, instance=instance, hashtag=hashtag
                            )
                            break

                        for item in valid:
                            external_id = str(item["external_id"])
                            if external_id in seen_ids or term_yielded >= term_limit:
                                continue
                            seen_ids.add(external_id)
                            yield RawContentItem(
                                external_id=external_id,
                                canonical_url=str(item["canonical_url"]),
                                title=str(item["title"]),
                                body_snippet=str(item["body"]),
                                author=str(item["author"]),
                                hashtags=list(item["hashtags"]),
                                locale=item["locale"],
                                published_at=item["published_at"],
                                metrics=dict(item["metrics"]),
                                raw_payload=dict(item["raw_payload"]),
                            )
                            yielded += 1
                            term_yielded += 1
                            instance_yielded += 1

                        if not next_cursor:
                            query.report_cursor(
                                "hashtag", None, instance=instance, hashtag=hashtag
                            )
                            break
                        if next_cursor in seen_cursors or next_cursor == max_id:
                            if query.warning_callback:
                                await query.warning_callback(
                                    "MASTODON_CURSOR_STALLED",
                                    f"Mastodon instance {instance} repeated its pagination cursor.",
                                )
                            break
                        seen_cursors.add(next_cursor)
                        query.report_cursor(
                            "hashtag", next_cursor, instance=instance, hashtag=hashtag
                        )
                        max_id = next_cursor
                        frontier = False

        if successful_requests == 0 and failures:
            raise failures[0]


def default_connectors() -> dict[str, SourceConnector]:
    connectors: list[SourceConnector] = [
        YouTubeConnector(),
        FeedConnector(
            "web",
            "Game news",
            "Public keyword news feed is available by default; override it with WEB_FEED_URLS.",
            settings.web_feed_urls or DEFAULT_WEB_FEED_URLS,
        ),
        SteamReviewsConnector(),
        BlueskyConnector(),
        MastodonConnector(),
        RedditConnector(),
        XConnector(),
    ]
    media_sources = [
        ("xhs", "Xiaohongshu", "xhs", ("like_count", "comment_count", "favorite_count")),
        ("douyin", "Douyin", "dy", ("like_count", "comment_count", "share_count", "view_count")),
        ("kuaishou", "Kuaishou", "ks", ("like_count", "comment_count", "share_count", "view_count")),
        ("bilibili", "Bilibili", "bili", ("like_count", "comment_count", "favorite_count", "view_count")),
        ("weibo", "Weibo", "wb", ("like_count", "comment_count", "share_count")),
        ("tieba", "Baidu Tieba", "tieba", ("comment_count",)),
        ("zhihu", "Zhihu", "zhihu", ("like_count", "comment_count", "favorite_count")),
    ]
    connectors.extend(
        JsonlCommandConnector(
            source_id, label,
            "Set MEDIACRAWLER_COMMAND to an explicit local adapter after you have logged in visibly.",
            ConnectorCapabilities(True, requires_login=True, interaction_fields=fields), platform,
        )
        for source_id, label, platform, fields in media_sources
    )
    connectors.extend(
        [
            TikTokDisplayConnector(),
            FacebookPageConnector(),
            InstagramHashtagConnector(),
        ]
    )
    if settings.content_bot_cbce_enabled:
        from .cbce_runtime import provider_overrides

        overrides = provider_overrides()
        from .cbce_connectors import (
            CbceBilibiliConnector,
            CbceLicensedWeiboConnector,
            CbceObservedDomConnector,
            CbceTiebaConnector,
        )

        selected_search_providers = {
            source_id: operations.get("search")
            for source_id, operations in overrides.items()
            if operations.get("search")
        }
        connectors = [
            (
                CbceLicensedWeiboConnector()
                if selected_search_providers.get(connector.source_id)
                == "licensed_weibo"
                else (
                    CbceTiebaConnector()
                    if connector.source_id == "tieba"
                    and selected_search_providers.get(connector.source_id)
                    == "cbce_tieba"
                    else (
                        CbceBilibiliConnector()
                        if connector.source_id == "bilibili"
                        and selected_search_providers.get(connector.source_id)
                        == "cbce_bilibili"
                        else CbceObservedDomConnector(connector.source_id)
                        if selected_search_providers.get(connector.source_id)
                        == f"cbce_{connector.source_id}"
                        else connector
                    )
                )
            )
            for connector in connectors
        ]
    result = {connector.source_id: connector for connector in connectors}
    if len(result) != len(connectors):
        raise ValueError("Duplicate connector source ID")
    from ..crawlers.registry import CANONICAL_SOURCE_IDS

    if tuple(result) != CANONICAL_SOURCE_IDS:
        raise ValueError("Connector registrations must match the canonical source registry")
    return result
