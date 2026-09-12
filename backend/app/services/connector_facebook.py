from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..config import settings
from ..crawlers.adapters.facebook import (
    FacebookGraphPageProvider,
    FacebookPageFeedAdapter,
    FacebookTargetKind,
    MetaPageGraphConfig,
    parse_facebook_target,
)
from ..crawlers.runtime import (
    CancellationToken,
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
                query.report_cursor("channel", page.next_cursor, page_id=config.page_id)
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
            credential("facebook_page_access_token", "") or "",
            credential("facebook_page_id", ""),
            credential("facebook_page_username", ""),
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
