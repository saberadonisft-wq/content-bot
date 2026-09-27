"""Provider-neutral Bilibili search contract and canonical normalizer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from ...runtime import (
    CancellationToken,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    RunContext,
)
from .targets import BilibiliTargetKind, parse_bilibili_target

_METRICS = {"view_count", "like_count", "comment_count", "favorite_count"}


@dataclass(frozen=True, slots=True)
class BilibiliVideo:
    video_id: str
    canonical_url: str
    title: str
    description: str = ""
    creator_id: str = ""
    published_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)
    media: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class BilibiliVideoPage:
    items: tuple[BilibiliVideo, ...]
    next_cursor: str | None
    has_more: bool


class BilibiliSearchProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliVideoPage: ...

    async def close(self) -> None: ...


class BilibiliDetailProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def fetch_detail(
        self,
        target: str,
        cancellation: CancellationToken,
    ) -> BilibiliVideo: ...

    async def close(self) -> None: ...


class BilibiliCreatorProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def creator_page(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliVideoPage: ...

    async def close(self) -> None: ...


class BilibiliSearchAdapter:
    source_id = "bilibili"
    provider_id = "cbce_bilibili"
    owns_resources = True

    def __init__(
        self,
        provider: BilibiliSearchProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if not context.terms:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili keyword search requires at least one term.",
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
            raise RuntimeError("Bilibili adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili checkpoint cursor has an unsupported shape.",
            )
        provider_page = await self.provider.search_page(
            context.terms,
            cursor,
            limit,
            self._cancellation,
        )
        records = tuple(
            normalize_bilibili_video(video, self.pseudonymizer)
            for video in provider_page.items
        )
        return Page(records, provider_page.next_cursor, provider_page.has_more)

    async def close(self) -> None:
        self._cancellation = None
        await self.provider.close()


class BilibiliDetailAdapter:
    source_id = "bilibili"
    provider_id = "cbce_bilibili"
    owns_resources = True

    def __init__(
        self,
        provider: BilibiliDetailProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._cancellation: CancellationToken | None = None
        self._target: str | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url")
        if not isinstance(target, str):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili detail requires a public video URL.",
            )
        try:
            parsed = parse_bilibili_target(target)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili detail target is invalid.",
            ) from exc
        if parsed.kind is not BilibiliTargetKind.VIDEO:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili detail target is not a video.",
            )
        self._target = parsed.media_url
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch(self) -> ContentRecord:
        if self._target is None or self._cancellation is None:
            raise RuntimeError("Bilibili detail adapter is not open")
        video = await self.provider.fetch_detail(
            self._target,
            self._cancellation,
        )
        return normalize_bilibili_video(video, self.pseudonymizer)

    async def close(self) -> None:
        self._target = None
        self._cancellation = None
        await self.provider.close()


class BilibiliCreatorAdapter:
    source_id = "bilibili"
    provider_id = "cbce_bilibili"
    owns_resources = True

    def __init__(
        self,
        provider: BilibiliCreatorProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._cancellation: CancellationToken | None = None
        self._target: str | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url")
        if not isinstance(target, str):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili creator listing requires a creator URL.",
            )
        try:
            parsed = parse_bilibili_target(target)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili creator target is invalid.",
            ) from exc
        if parsed.kind is not BilibiliTargetKind.CREATOR:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili creator target is not a creator page.",
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
            raise RuntimeError("Bilibili creator adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili creator checkpoint has an unsupported shape.",
            )
        provider_page = await self.provider.creator_page(
            self._target,
            cursor,
            limit,
            self._cancellation,
        )
        records = tuple(
            normalize_bilibili_video(video, self.pseudonymizer)
            for video in provider_page.items
        )
        return Page(records, provider_page.next_cursor, provider_page.has_more)

    async def close(self) -> None:
        self._target = None
        self._cancellation = None
        await self.provider.close()


def normalize_bilibili_video(
    video: BilibiliVideo,
    pseudonymizer: IdentityPseudonymizer,
) -> ContentRecord:
    try:
        target = parse_bilibili_target(video.canonical_url)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bilibili provider emitted an invalid canonical video URL.",
        ) from exc
    if (
        target.kind is not BilibiliTargetKind.VIDEO
        or target.external_id != video.video_id
    ):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bilibili provider video identity is inconsistent.",
        )
    title = video.title.strip()
    if not title:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bilibili provider emitted a video without a title.",
        )
    published_at = video.published_at
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    metrics = {
        key: value
        for key, value in video.metrics.items()
        if key in _METRICS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    media = tuple(
        normalized
        for item in video.media
        if (normalized := _normalize_media_metadata(item)) is not None
    )
    return ContentRecord(
        source_id="bilibili",
        external_id=video.video_id,
        canonical_url=target.canonical_url,
        title=title,
        body=video.description.strip(),
        author_pseudonym=pseudonymizer.pseudonym("bilibili", video.creator_id),
        published_at=published_at,
        metrics=metrics,
        media=media,
        provenance={
            "provider_id": "cbce_bilibili",
            "contract_version": "cbce.bilibili.video.v1",
        },
    )


def _normalize_media_metadata(
    item: Mapping[str, object],
) -> Mapping[str, object] | None:
    kind = str(item.get("kind") or "").strip()
    if kind == "cover":
        url = normalize_bilibili_cover_url(item.get("url"))
        if url is None:
            return None
        return {"kind": "cover", "url": url}
    if kind == "video_metadata":
        duration = item.get("duration_seconds")
        if (
            isinstance(duration, int)
            and not isinstance(duration, bool)
            and 0 < duration <= 86_400
        ):
            return {"kind": "video_metadata", "duration_seconds": duration}
    return None


def normalize_bilibili_cover_url(value: object) -> str | None:
    """Keep only public HTTPS cover URLs emitted by the visible provider."""

    if not isinstance(value, str):
        return None
    url = value.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
