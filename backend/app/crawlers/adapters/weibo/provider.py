"""Provider-neutral Weibo post contract and bounded search adapter."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ...runtime import (
    CancellationToken,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    RunContext,
)
from .targets import WeiboTargetKind, parse_weibo_target


@dataclass(frozen=True, slots=True)
class WeiboPost:
    post_id: str
    canonical_url: str
    text: str
    author_id: str = ""
    published_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)
    media: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class WeiboPostPage:
    items: tuple[WeiboPost, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class WeiboSearchCursor:
    term_index: int = 0
    page_number: int = 1
    offset: int = 0

    def __post_init__(self) -> None:
        if self.term_index < 0 or self.page_number < 1 or self.offset < 0:
            raise ValueError("Invalid Weibo search cursor")

    def encode(self) -> str:
        return f"v1w:{self.term_index}:{self.page_number}:{self.offset}"

    @classmethod
    def decode(cls, value: str | None) -> WeiboSearchCursor:
        if value is None:
            return cls()
        match = re.fullmatch(r"v1w:([0-9]+):([1-9][0-9]*):([0-9]+)", value)
        if not match:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Weibo search checkpoint cursor is invalid.",
            )
        return cls(*(int(part) for part in match.groups()))


class WeiboSearchProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> WeiboPostPage: ...

    async def close(self) -> None: ...


class WeiboSearchAdapter:
    source_id = "weibo"
    provider_id = "cbce_weibo"
    owns_resources = True

    def __init__(
        self,
        provider: WeiboSearchProvider,
        pseudonymizer: IdentityPseudonymizer,
        *,
        provider_id: str = "cbce_weibo",
    ) -> None:
        if not provider_id.strip():
            raise ValueError("Weibo adapter provider identity cannot be empty")
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self.provider_id = provider_id
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if not context.terms:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Weibo search requires at least one term.",
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
            raise RuntimeError("Weibo adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Weibo search checkpoint has an unsupported shape.",
            )
        page = await self.provider.search_page(
            context.terms,
            cursor,
            limit,
            self._cancellation,
        )
        return Page(
            tuple(
                normalize_weibo_post(
                    post,
                    self.pseudonymizer,
                    provider_id=self.provider_id,
                )
                for post in page.items
            ),
            page.next_cursor,
            page.has_more,
        )

    async def close(self) -> None:
        self._cancellation = None
        await self.provider.close()


def normalize_weibo_post(
    post: WeiboPost,
    pseudonymizer: IdentityPseudonymizer,
    *,
    provider_id: str = "cbce_weibo",
) -> ContentRecord:
    if not provider_id.strip():
        raise ValueError("Weibo record provider identity cannot be empty")
    try:
        target = parse_weibo_target(post.canonical_url)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Weibo provider emitted an invalid public URL.",
        ) from exc
    if target.kind is not WeiboTargetKind.POST or target.external_id != post.post_id:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Weibo provider post identity is inconsistent.",
        )
    text = post.text.strip()
    if not text:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Weibo provider emitted a post without text.",
        )
    published_at = post.published_at
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    metrics = {
        key: value
        for key, value in post.metrics.items()
        if key in {"like_count", "comment_count", "share_count"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    media = tuple(
        {"kind": "image", "url": str(item["url"])}
        for item in post.media
        if item.get("kind") == "image"
        and str(item.get("url") or "").startswith("https://")
    )
    title = next((line.strip() for line in text.splitlines() if line.strip()), text)[
        :180
    ]
    return ContentRecord(
        source_id="weibo",
        external_id=post.post_id,
        canonical_url=target.canonical_url,
        title=title,
        body=text[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("weibo", post.author_id),
        published_at=published_at,
        metrics=metrics,
        media=media,
        provenance={
            "provider_id": provider_id,
            "contract_version": "cbce.weibo.post.v1",
        },
    )
