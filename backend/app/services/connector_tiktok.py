from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import settings
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
                    if (
                        datetime.now(UTC) + timedelta(minutes=5)
                        >= stored.access_expires_at
                    ):
                        if stored.refresh_expired:
                            raise RuntimeError("TikTok OAuth refresh token has expired")
                        if not self._oauth_configured():
                            raise RuntimeError(
                                "TikTok OAuth client settings are required to refresh access"
                            )
                        oauth = TikTokOAuthClient(
                            TikTokOAuthConfig(
                                credential("tiktok_client_key", ""),
                                credential("tiktok_client_secret", "") or "",
                                credential("tiktok_redirect_uri", ""),
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
            credential("tiktok_client_key")
            and credential("tiktok_client_secret")
            and credential("tiktok_redirect_uri")
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
