from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from ..config import settings
from ..crawlers.adapters.instagram import (
    InstagramGraphHashtagProvider,
    InstagramHashtagBudget,
    InstagramHashtagSearchAdapter,
    MetaGraphConfig,
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
            term for term in query.search_terms if self._is_hashtag_term(term)
        )
        if not terms:
            raise RuntimeError(
                "Instagram hashtag discovery requires a single-token hashtag such as #gamedev"
            )
        initial_cursor = query.resume_cursor("query", default=None, legacy_key="cursor")
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
            credential("meta_access_token", "") or "",
            credential("instagram_professional_user_id", ""),
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
        return bool(
            normalized
            and len(normalized) <= 100
            and not any(character.isspace() for character in normalized)
            and "#" not in normalized
        )

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
