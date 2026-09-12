from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import settings
from ..crawlers.adapters.reddit import (
    RedditApiCommentProvider,
    RedditCommentBudgets,
    RedditCommentsAdapter,
    RedditCommentScan,
)
from ..crawlers.runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    PseudonymKeyStore,
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
    get_with_retries,
)
from .credential_resolver import credential
from .reddit_oauth import reddit_token_cache


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
        return bool(
            credential("reddit_client_id") and credential("reddit_client_secret")
        )

    async def healthcheck(self) -> ConnectorStatus:
        if not credential("reddit_client_id") or not credential("reddit_client_secret"):
            return ConnectorStatus(
                "not_configured",
                "Add REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET for official Reddit OAuth search.",
            )
        return ConnectorStatus(
            "ready", "Official Reddit OAuth keyword search is configured."
        )

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        """Search public Reddit submissions through application-only OAuth."""
        if not credential("reddit_client_id") or not credential("reddit_client_secret"):
            return
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        yielded = 0
        seen_posts: set[str] = set()
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            headers={
                "User-Agent": credential(
                    "reddit_user_agent", "ContentBot/0.1 (local research tool)"
                )
            },
        ) as client:
            access_token = await reddit_token_cache.get(
                client,
                client_id=credential("reddit_client_id"),
                client_secret=credential("reddit_client_secret"),
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
                        query.report_cursor("term", after or None, term=search_term)
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
                    query.report_cursor("term", next_after or None, term=search_term)
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
        if not credential("reddit_client_id") or not credential("reddit_client_secret"):
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Reddit OAuth credentials are not configured.",
            )
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            headers={
                "User-Agent": credential(
                    "reddit_user_agent", "ContentBot/0.1 (local research tool)"
                )
            },
        ) as client:
            access_token = await reddit_token_cache.get(
                client,
                client_id=credential("reddit_client_id"),
                client_secret=credential("reddit_client_secret"),
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
            return (
                datetime.fromtimestamp(float(value), tz=UTC)
                if value is not None
                else None
            )
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _pseudonymizer() -> IdentityPseudonymizer:
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        return IdentityPseudonymizer(key_store.load_or_create())
