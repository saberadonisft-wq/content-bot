from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import settings
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
)
from .connector_contracts import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from .connector_support import (
    pooled_client,
)
from .credential_resolver import credential
from .youtube_api import YouTubeQuotaBudget, is_invalid_page_token, youtube_json


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
        return bool(credential("youtube_api_key"))

    async def healthcheck(self) -> ConnectorStatus:
        if not credential("youtube_api_key"):
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
        if not credential("youtube_api_key"):
            return await self.healthcheck()
        try:
            async with pooled_client(
                base_url="https://www.googleapis.com/youtube/v3",
                timeout=30,
                follow_redirects=False,
            ) as client:
                await youtube_json(
                    client,
                    "/i18nLanguages",
                    params={"part": "snippet", "key": credential("youtube_api_key")},
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
        if not credential("youtube_api_key"):
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "YouTube Data API key is not configured.",
            )
        async with pooled_client(
            base_url="https://www.googleapis.com/youtube/v3",
            timeout=30,
            follow_redirects=False,
        ) as client:
            provider = YouTubeApiCommentProvider(
                client,
                api_key=credential("youtube_api_key"),
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

    async def search(
        self, query: SearchQuery, checkpoint: dict[str, Any] | None = None
    ) -> AsyncIterator[RawContentItem]:
        if not credential("youtube_api_key"):
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
        published_after = (
            (datetime.now(UTC) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
        )
        async with pooled_client(
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
                    "key": credential("youtube_api_key"),
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
                video_ids = [
                    row.get("id", {}).get("videoId")
                    for row in rows
                    if row.get("id", {}).get("videoId")
                ]
                if not video_ids:
                    query.report_cursor("query", None)
                    break
                page_has_unseen = any(
                    video_id not in known_ids for video_id in video_ids
                )
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
                    params={
                        "part": "snippet,statistics",
                        "id": ",".join(video_ids),
                        "key": credential("youtube_api_key"),
                    },
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
                    published_at = (
                        datetime.fromisoformat(published) if published else None
                    )
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
