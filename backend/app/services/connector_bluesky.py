from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..config import settings
from ..crawlers.adapters.bluesky import (
    BlueskyApiCommentProvider,
    BlueskyCommentBudgets,
    BlueskyCommentsAdapter,
    BlueskyCommentScan,
)
from ..crawlers.runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    PseudonymKeyStore,
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
from .connector_contracts import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from .connector_support import (
    allocate_limits,
    pooled_client,
    stable_external_id,
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
        return ConnectorStatus(
            "ready", "Public Bluesky keyword search; no account or API key required."
        )

    async def scan_comments(
        self,
        target_url: str,
        budgets: BlueskyCommentBudgets,
        *,
        sort: str = "provider",
        cancellation: CancellationToken | None = None,
    ) -> BlueskyCommentScan:
        async with pooled_client(
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
        async with pooled_client(
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
