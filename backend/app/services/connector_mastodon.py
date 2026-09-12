from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings
from ..crawlers.adapters.mastodon import (
    MastodonApiCommentProvider,
    MastodonCommentBudgets,
    MastodonCommentsAdapter,
    MastodonCommentScan,
    parse_mastodon_status_target,
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
    pooled_client,
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
                async with pooled_client(
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
        target = parse_mastodon_status_target(target_url, allowed_instances=instances)
        async with pooled_client(
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

        for search_term, term_limit in zip(
            query.search_terms, term_limits, strict=True
        ):
            hashtag = mastodon_hashtag(search_term)
            if term_limit <= 0 or not hashtag:
                continue
            term_yielded = 0
            instance_limits = allocate_limits(term_limit, len(instances))
            for instance, instance_limit in zip(
                instances, instance_limits, strict=True
            ):
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
                async with pooled_client(
                    base_url=f"https://{instance}",
                    timeout=30,
                    follow_redirects=False,
                    headers={"User-Agent": "ContentBot/0.1 (Mastodon public API)"},
                ) as client:
                    while (
                        term_yielded < term_limit and instance_yielded < instance_limit
                    ):
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
