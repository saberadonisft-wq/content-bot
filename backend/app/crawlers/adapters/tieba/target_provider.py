"""Project-observed Tieba forum, creator and thread-detail DOM providers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ...runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure, RunContext
from ..browser_session import OwnedBrowserPage
from .dom_provider import TiebaDomContract, extract_tieba_dom_threads
from .provider import TiebaThread, TiebaThreadPage
from .targets import TiebaTarget, TiebaTargetKind, parse_tieba_target

BrowserEventHandler = Callable[[], Awaitable[None] | None]

_DETAIL_SCRIPT = r"""
({contract}) => {
  const title = document.querySelector(contract.title_selector);
  const root = document.querySelector(contract.root_selector);
  const body = root && contract.body_selector
    ? root.querySelector(contract.body_selector)
    : null;
  const login = contract.login_selector
    ? document.querySelector(contract.login_selector)
    : null;
  return {
    recognized: Boolean(title && root),
    auth_required: Boolean(login && !(title && root)),
    title: title ? title.innerText : '',
    body: body ? body.innerText : '',
  };
}
"""


@dataclass(frozen=True, slots=True)
class TiebaTargetCursor:
    kind: TiebaTargetKind
    target_digest: str
    offset: int = 0

    def encode(self) -> str:
        return f"v1t:{self.kind.value}:{self.target_digest}:{self.offset}"

    @classmethod
    def decode(cls, target: TiebaTarget, value: str | None) -> TiebaTargetCursor:
        digest = _target_digest(target)
        if value is None:
            return cls(target.kind, digest)
        match = re.fullmatch(r"v1t:(forum|creator):([0-9a-f]{16}):([0-9]+)", value)
        if (
            not match
            or match.group(1) != target.kind.value
            or match.group(2) != digest
        ):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba target-list checkpoint is invalid.",
            )
        return cls(target.kind, digest, int(match.group(3)))


@dataclass(frozen=True, slots=True)
class TiebaDetailDomContract:
    title_selector: str
    root_selector: str
    body_selector: str
    login_selector: str = ""

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 300
            for value in (self.title_selector, self.root_selector, self.body_selector)
        ):
            raise ValueError("Tieba detail DOM contract is invalid")
        if self.login_selector and len(self.login_selector) > 300:
            raise ValueError("Tieba detail login selector is invalid")

    def as_browser_payload(self) -> dict[str, str]:
        return {
            "title_selector": self.title_selector,
            "root_selector": self.root_selector,
            "body_selector": self.body_selector,
            "login_selector": self.login_selector,
        }


class TiebaDomTargetProvider:
    def __init__(
        self,
        browser_page: OwnedBrowserPage,
        *,
        forum_contract: TiebaDomContract,
        creator_contract: TiebaDomContract,
        detail_contract: TiebaDetailDomContract,
        auth_timeout_seconds: float = 600,
        on_auth_required: BrowserEventHandler | None = None,
        on_authenticated: BrowserEventHandler | None = None,
    ) -> None:
        if not 1 <= auth_timeout_seconds <= 7_200:
            raise ValueError("Tieba auth timeout must be between 1 and 7200 seconds")
        self.browser_page = browser_page
        self.contracts = {
            TiebaTargetKind.FORUM: forum_contract,
            TiebaTargetKind.CREATOR: creator_contract,
        }
        self.detail_contract = detail_contract
        self.auth_timeout_seconds = auth_timeout_seconds
        self.on_auth_required = on_auth_required
        self.on_authenticated = on_authenticated
        self._context: RunContext | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != "tieba" or context.operation not in {
            "fetch_detail",
            "scan_channel",
            "list_creator",
        }:
            raise ValueError("Tieba target provider context is invalid")
        self._context = context
        await self.browser_page.open(context, cancellation)

    async def fetch_detail(
        self,
        target: str,
        cancellation: CancellationToken,
    ) -> TiebaThread:
        parsed = _target(target, {TiebaTargetKind.THREAD})
        page = await self._navigate(parsed.canonical_url, cancellation)
        raw = await page.evaluate(
            _DETAIL_SCRIPT,
            {"contract": self.detail_contract.as_browser_payload()},
        )
        if not isinstance(raw, Mapping):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba detail DOM result is invalid.",
            )
        if raw.get("auth_required") is True:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Log in to Tieba in its visible application profile.",
            )
        if raw.get("recognized") is not True:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba thread-detail DOM layout was not recognized.",
            )
        title = str(raw.get("title") or "").strip()
        if not title:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba thread-detail title is empty.",
            )
        return TiebaThread(
            parsed.external_id,
            parsed.canonical_url,
            title[:500],
            body=str(raw.get("body") or "").strip()[:10_000],
        )

    async def target_page(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> TiebaThreadPage:
        if not 1 <= limit <= 500:
            raise ValueError("Tieba target page limit is invalid")
        parsed = _target(target, set(self.contracts))
        state = TiebaTargetCursor.decode(parsed, cursor)
        page = await self._navigate(parsed.canonical_url, cancellation)
        items, observed_more = await extract_tieba_dom_threads(
            page,
            self.contracts[parsed.kind],
            cancellation,
            layout_label=f"{parsed.kind.value} listing",
        )
        start = min(state.offset, len(items))
        stop = min(len(items), start + limit)
        next_state = (
            TiebaTargetCursor(parsed.kind, state.target_digest, stop)
            if stop < len(items)
            else None
        )
        # No trustworthy infinite-scroll continuation has been observed. If the
        # page starts advertising one, fail closed rather than silently truncate.
        if observed_more and next_state is None:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba target listing exposed unimplemented pagination.",
            )
        return TiebaThreadPage(
            tuple(items[start:stop]),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._context = None
        await self.browser_page.close()

    async def _navigate(self, url: str, cancellation: CancellationToken) -> Any:
        if self._context is None:
            raise RuntimeError("Tieba target provider is not open")
        return await self.browser_page.navigate(
            url,
            cancellation,
            wait_after_ms=4_000,
            auth_timeout_seconds=self.auth_timeout_seconds,
            on_auth_required=self.on_auth_required,
            on_authenticated=self.on_authenticated,
        )


def _target(value: str, kinds: set[TiebaTargetKind]) -> TiebaTarget:
    try:
        parsed = parse_tieba_target(value)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Tieba target URL is invalid.",
        ) from exc
    if parsed.kind not in kinds:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Tieba target kind is unsupported for this operation.",
        )
    return parsed


def _target_digest(target: TiebaTarget) -> str:
    return hashlib.sha256(target.canonical_url.encode("utf-8")).hexdigest()[:16]


TIEBA_FORUM_DOM_CONTRACT = TiebaDomContract(
    root_selector=".frs-feed-list",
    card_selector=".thread-card",
    link_selector="a.action-link-bg",
    title_selector=".thread-title",
    body_selector=".thread-content",
    author_selector="a.head-name",
    comment_count_selector="a.comment-link-zone .action-number",
)

TIEBA_CREATOR_DOM_CONTRACT = TiebaDomContract(
    root_selector=".thread-list-wrapper",
    card_selector=".thread-card",
    link_selector="a.action-link-bg",
    title_selector=".thread-title",
    body_selector=".thread-content",
    author_selector="a.head-name",
    comment_count_selector="a.comment-link-zone .action-number",
)

TIEBA_DETAIL_DOM_CONTRACT = TiebaDetailDomContract(
    title_selector="span.pb-title",
    root_selector=".comment-content",
    body_selector=".pb-rich-text",
    login_selector=".login-btn",
)
