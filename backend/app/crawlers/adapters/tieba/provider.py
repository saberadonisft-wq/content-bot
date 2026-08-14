"""Provider-neutral Tieba thread contract and bounded search adapter."""

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
from .targets import TiebaTargetKind, parse_tieba_target


@dataclass(frozen=True, slots=True)
class TiebaThread:
    thread_id: str
    canonical_url: str
    title: str
    body: str = ""
    author_id: str = ""
    published_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TiebaThreadPage:
    items: tuple[TiebaThread, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class TiebaSearchCursor:
    term_index: int = 0
    page_number: int = 1
    offset: int = 0

    def __post_init__(self) -> None:
        if self.term_index < 0 or self.page_number < 1 or self.offset < 0:
            raise ValueError("Invalid Tieba search cursor")

    def encode(self) -> str:
        return f"v1b:{self.term_index}:{self.page_number}:{self.offset}"

    @classmethod
    def decode(cls, value: str | None) -> TiebaSearchCursor:
        if value is None:
            return cls()
        match = re.fullmatch(r"v1b:([0-9]+):([1-9][0-9]*):([0-9]+)", value)
        if not match:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba search checkpoint cursor is invalid.",
            )
        return cls(*(int(part) for part in match.groups()))


class TiebaSearchProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> TiebaThreadPage: ...

    async def close(self) -> None: ...


class TiebaDetailProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def fetch_detail(
        self,
        target: str,
        cancellation: CancellationToken,
    ) -> TiebaThread: ...

    async def close(self) -> None: ...


class TiebaTargetThreadsProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def target_page(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> TiebaThreadPage: ...

    async def close(self) -> None: ...


class TiebaSearchAdapter:
    source_id = "tieba"
    provider_id = "cbce_tieba"
    owns_resources = True

    def __init__(
        self,
        provider: TiebaSearchProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if not context.terms:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba search requires at least one term.",
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
            raise RuntimeError("Tieba adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba search checkpoint has an unsupported shape.",
            )
        page = await self.provider.search_page(
            context.terms,
            cursor,
            limit,
            self._cancellation,
        )
        return Page(
            tuple(
                normalize_tieba_thread(thread, self.pseudonymizer)
                for thread in page.items
            ),
            page.next_cursor,
            page.has_more,
        )

    async def close(self) -> None:
        self._cancellation = None
        await self.provider.close()


class TiebaDetailAdapter:
    source_id = "tieba"
    provider_id = "cbce_tieba"
    owns_resources = True

    def __init__(
        self,
        provider: TiebaDetailProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._target: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url")
        if context.operation != "fetch_detail" or not isinstance(target, str):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba detail requires a public thread URL.",
            )
        try:
            parsed = parse_tieba_target(target)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba detail target is invalid.",
            ) from exc
        if parsed.kind is not TiebaTargetKind.THREAD:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba detail target is not a thread.",
            )
        self._target = parsed.canonical_url
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch(self) -> ContentRecord:
        if self._target is None or self._cancellation is None:
            raise RuntimeError("Tieba detail adapter is not open")
        thread = await self.provider.fetch_detail(
            self._target,
            self._cancellation,
        )
        return normalize_tieba_thread(thread, self.pseudonymizer)

    async def close(self) -> None:
        self._target = None
        self._cancellation = None
        await self.provider.close()


class TiebaTargetThreadsAdapter:
    source_id = "tieba"
    provider_id = "cbce_tieba"
    owns_resources = True

    def __init__(
        self,
        provider: TiebaTargetThreadsProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._target: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url")
        if context.operation not in {"scan_channel", "list_creator"} or not isinstance(
            target, str
        ):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba target listing requires a forum or creator URL.",
            )
        try:
            parsed = parse_tieba_target(target)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba target listing URL is invalid.",
            ) from exc
        expected = (
            {TiebaTargetKind.FORUM, TiebaTargetKind.CREATOR}
            if context.operation == "scan_channel"
            else {TiebaTargetKind.CREATOR}
        )
        if parsed.kind not in expected:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba target kind does not match the requested operation.",
            )
        self._target = parsed.canonical_url
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[ContentRecord, str]:
        if self._target is None or self._cancellation is None:
            raise RuntimeError("Tieba target listing adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba target listing checkpoint has an unsupported shape.",
            )
        page = await self.provider.target_page(
            self._target,
            cursor,
            limit,
            self._cancellation,
        )
        return Page(
            tuple(
                normalize_tieba_thread(thread, self.pseudonymizer)
                for thread in page.items
            ),
            page.next_cursor,
            page.has_more,
        )

    async def close(self) -> None:
        self._target = None
        self._cancellation = None
        await self.provider.close()


def normalize_tieba_thread(
    thread: TiebaThread,
    pseudonymizer: IdentityPseudonymizer,
) -> ContentRecord:
    try:
        target = parse_tieba_target(thread.canonical_url)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Tieba provider emitted an invalid public URL.",
        ) from exc
    if target.kind is not TiebaTargetKind.THREAD or target.external_id != thread.thread_id:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Tieba provider thread identity is inconsistent.",
        )
    title = thread.title.strip()
    if not title:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Tieba provider emitted a thread without a title.",
        )
    published_at = thread.published_at
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    comment_count = thread.metrics.get("comment_count")
    metrics = (
        {"comment_count": comment_count}
        if isinstance(comment_count, int)
        and not isinstance(comment_count, bool)
        and comment_count >= 0
        else {}
    )
    return ContentRecord(
        source_id="tieba",
        external_id=thread.thread_id,
        canonical_url=target.canonical_url,
        title=title,
        body=thread.body.strip()[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("tieba", thread.author_id),
        published_at=published_at,
        metrics=metrics,
        provenance={
            "provider_id": "cbce_tieba",
            "contract_version": "cbce.tieba.thread.v1",
        },
    )
