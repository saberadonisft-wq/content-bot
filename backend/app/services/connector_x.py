from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..crawlers.adapters.x import (
    XApiClient,
    XCommentsAdapter,
    XCreatorAdapter,
    XDetailAdapter,
    XSearchAdapter,
    XTargetKind,
    parse_x_target,
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
from .connector_contracts import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from .credential_resolver import credential


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
        return bool(credential("x_bearer_token"))

    async def healthcheck(self) -> ConnectorStatus:
        if not credential("x_bearer_token"):
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
        token = credential("x_bearer_token")
        if not token:
            return
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        initial_cursor = query.resume_cursor("query", default=None, legacy_key="cursor")
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
        token = credential("x_bearer_token")
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
        token = credential("x_bearer_token")
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
        token = credential("x_bearer_token")
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
        token = credential("x_bearer_token")
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
            truncated=bool(unresolved or has_more),
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
