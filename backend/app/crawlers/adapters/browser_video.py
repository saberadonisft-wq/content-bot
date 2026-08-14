"""Shared provider-neutral video/post adapter for browser-session platforms."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..runtime import (
    CancellationToken,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    RunContext,
)


@dataclass(frozen=True, slots=True)
class BrowserVideo:
    external_id: str
    canonical_url: str
    title: str
    body: str = ""
    author_id: str = ""
    published_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)
    media: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserVideoPage:
    items: tuple[BrowserVideo, ...]
    next_cursor: str | None
    has_more: bool


class BrowserVideoSearchProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BrowserVideoPage: ...

    async def close(self) -> None: ...


Canonicalizer = Callable[[str], Any]
IdentityBuilder = Callable[[Any], str]


class BrowserVideoSearchAdapter:
    owns_resources = True

    def __init__(
        self,
        *,
        source_id: str,
        provider_id: str,
        provider: BrowserVideoSearchProvider,
        pseudonymizer: IdentityPseudonymizer,
        canonicalize: Canonicalizer,
        content_kinds: frozenset[str],
        metric_ids: frozenset[str],
        identity_builder: IdentityBuilder | None = None,
    ) -> None:
        if not source_id or not provider_id or not content_kinds:
            raise ValueError("Browser video adapter identity cannot be empty")
        self.source_id = source_id
        self.provider_id = provider_id
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self.canonicalize = canonicalize
        self.content_kinds = content_kinds
        self.metric_ids = metric_ids
        self.identity_builder = identity_builder or _target_identity
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != self.source_id or context.provider_id != self.provider_id:
            raise ValueError("Browser video adapter identity does not match run context")
        if not context.terms:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                f"{self.source_id} search requires at least one term.",
            )
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[ContentRecord, str]:
        if self._cancellation is None:
            raise RuntimeError("Browser video adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                f"{self.source_id} checkpoint has an unsupported shape.",
            )
        page = await self.provider.search_page(
            context.terms, cursor, limit, self._cancellation
        )
        records = tuple(self._normalize(item) for item in page.items)
        return Page(records, page.next_cursor, page.has_more)

    async def close(self) -> None:
        self._cancellation = None
        await self.provider.close()

    def _normalize(self, item: BrowserVideo) -> ContentRecord:
        try:
            target = self.canonicalize(item.canonical_url)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                f"{self.source_id} provider emitted an invalid public URL.",
            ) from exc
        content_kind = str(getattr(getattr(target, "kind", None), "value", ""))
        if content_kind not in self.content_kinds or self.identity_builder(target) != item.external_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                f"{self.source_id} provider content identity is inconsistent.",
            )
        title = item.title.strip()
        if not title:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                f"{self.source_id} provider emitted content without a title.",
            )
        published_at = item.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        metrics = {
            key: value
            for key, value in item.metrics.items()
            if key in self.metric_ids
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        media = tuple(
            normalized
            for raw in item.media
            if (normalized := _normalize_media(raw)) is not None
        )
        return ContentRecord(
            source_id=self.source_id,
            external_id=item.external_id,
            canonical_url=str(target.canonical_url),
            title=title,
            body=item.body.strip()[:4_000],
            author_pseudonym=self.pseudonymizer.pseudonym(
                self.source_id, item.author_id
            ),
            published_at=published_at,
            metrics=metrics,
            media=media,
            provenance={
                "provider_id": self.provider_id,
                "contract_version": f"cbce.{self.source_id}.content.v1",
                "content_kind": content_kind,
            },
        )


def _target_identity(target: Any) -> str:
    return str(getattr(target, "external_id", ""))


def _normalize_media(item: Mapping[str, object]) -> Mapping[str, object] | None:
    kind = str(item.get("kind") or "")
    url = str(item.get("url") or "")
    if kind not in {"image", "video_cover", "video_metadata"}:
        return None
    if kind != "video_metadata" and not url.startswith("https://"):
        return None
    if kind == "video_metadata":
        duration = item.get("duration_seconds")
        if not isinstance(duration, int) or isinstance(duration, bool):
            return None
        if not 0 < duration <= 86_400:
            return None
        return {"kind": kind, "duration_seconds": duration}
    return {"kind": kind, "url": url}
