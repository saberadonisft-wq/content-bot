"""Isolated CBCE worker entry point.

Stdout is reserved for ``cbce.worker.v1`` NDJSON. Diagnostics go to stderr
after redaction. The first implemented worker action is bounded public Bilibili
DOM search.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SOURCE_REGISTRY
from .adapters import NavigationPolicy, OwnedBrowserPage
from .adapters.bilibili import (
    BilibiliChildCommentsAdapter,
    BilibiliCommentsAdapter,
    BilibiliCreatorAdapter,
    BilibiliDetailAdapter,
    BilibiliDomCommentsProvider,
    BilibiliDomCreatorProvider,
    BilibiliDomDetailProvider,
    BilibiliDomSearchProvider,
    BilibiliSearchAdapter,
    BilibiliTargetKind,
    parse_bilibili_target,
)
from .adapters.browser_video import BrowserVideoSearchAdapter
from .adapters.browser_video_dom import (
    BrowserVideoDomContract,
    BrowserVideoDomSearchProvider,
)
from .adapters.tieba import (
    TIEBA_CREATOR_DOM_CONTRACT,
    TIEBA_DETAIL_DOM_CONTRACT,
    TIEBA_FORUM_DOM_CONTRACT,
    TIEBA_SEARCH_DOM_CONTRACT,
    TiebaCommentsAdapter,
    TiebaDetailAdapter,
    TiebaDomCommentsProvider,
    TiebaDomSearchProvider,
    TiebaDomTargetProvider,
    TiebaSearchAdapter,
    TiebaTargetKind,
    TiebaTargetThreadsAdapter,
    parse_tieba_target,
)
from .adapters.weibo import (
    WeiboDomContract,
    WeiboDomSearchProvider,
    WeiboSearchAdapter,
)
from .observed_dom_contract import load_observed_dom_contract
from .observed_dom_runtime import OBSERVED_DOM_RUNTIME_SPECS
from .runtime import (
    BrowserLaunchRequest,
    BrowserSession,
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    PlaywrightPersistentDriver,
    ProfileNamespace,
    PseudonymKeyStore,
    RunBudgets,
    RunContext,
    WorkerEnvelope,
    WorkerMessageKind,
    bounded_pages,
    decode_message,
    encode_message,
    safe_diagnostic,
)
from .runtime.adapter import SearchAdapter


@dataclass(frozen=True, slots=True)
class BilibiliWorkerRequest:
    terms: tuple[str, ...]
    max_items: int
    max_requests: int
    deadline_seconds: float
    initial_cursor: str | None
    browser_executable: Path
    profile_root: Path
    pseudonym_key_ref: Path

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BilibiliWorkerRequest:
        if payload.get("action") != "bilibili_search":
            raise ValueError("Unsupported CBCE worker action")
        terms_value = payload.get("terms")
        if not isinstance(terms_value, (list, tuple)):
            raise TypeError("Worker search terms must be an array")
        terms = tuple(
            dict.fromkeys(
                str(term).strip() for term in terms_value if str(term).strip()
            )
        )
        if not terms or len(terms) > 20 or any(len(term) > 200 for term in terms):
            raise ValueError("Worker search terms are invalid")
        max_items = int(payload.get("max_items") or 0)
        max_requests = int(payload.get("max_requests") or 0)
        deadline = float(payload.get("deadline_seconds") or 0)
        if not 1 <= max_items <= 10_000:
            raise ValueError("Worker item budget is invalid")
        if not 1 <= max_requests <= 1_000:
            raise ValueError("Worker request budget is invalid")
        if not 1 <= deadline <= 7_200:
            raise ValueError("Worker deadline is invalid")
        cursor = payload.get("initial_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("Worker cursor must be a string")
        return cls(
            terms,
            max_items,
            max_requests,
            deadline,
            cursor,
            _required_path(payload, "browser_executable"),
            _required_path(payload, "profile_root"),
            _required_path(payload, "pseudonym_key_ref"),
        )


@dataclass(frozen=True, slots=True)
class TiebaWorkerRequest:
    terms: tuple[str, ...]
    max_items: int
    max_requests: int
    deadline_seconds: float
    initial_cursor: str | None
    browser_executable: Path
    profile_root: Path
    pseudonym_key_ref: Path

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TiebaWorkerRequest:
        if payload.get("action") != "tieba_search":
            raise ValueError("Unsupported CBCE Tieba worker action")
        terms_value = payload.get("terms")
        if not isinstance(terms_value, (list, tuple)):
            raise TypeError("Worker search terms must be an array")
        terms = tuple(
            dict.fromkeys(
                str(term).strip() for term in terms_value if str(term).strip()
            )
        )
        if not terms or len(terms) > 20 or any(len(term) > 200 for term in terms):
            raise ValueError("Worker search terms are invalid")
        max_items = int(payload.get("max_items") or 0)
        max_requests = int(payload.get("max_requests") or 0)
        deadline = float(payload.get("deadline_seconds") or 0)
        if not 1 <= max_items <= 10_000:
            raise ValueError("Worker item budget is invalid")
        if not 1 <= max_requests <= 1_000:
            raise ValueError("Worker request budget is invalid")
        if not 1 <= deadline <= 7_200:
            raise ValueError("Worker deadline is invalid")
        cursor = payload.get("initial_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("Worker cursor must be a string")
        return cls(
            terms,
            max_items,
            max_requests,
            deadline,
            cursor,
            _required_path(payload, "browser_executable"),
            _required_path(payload, "profile_root"),
            _required_path(payload, "pseudonym_key_ref"),
        )


@dataclass(frozen=True, slots=True)
class ObservedDomWorkerRequest:
    source_id: str
    terms: tuple[str, ...]
    max_items: int
    max_requests: int
    deadline_seconds: float
    initial_cursor: str | None
    browser_executable: Path
    profile_root: Path
    pseudonym_key_ref: Path
    contract_root: Path

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ObservedDomWorkerRequest:
        action = str(payload.get("action") or "")
        source_id = action.removesuffix("_search")
        if (
            action != f"{source_id}_search"
            or source_id not in OBSERVED_DOM_RUNTIME_SPECS
        ):
            raise ValueError("Unsupported reviewed DOM worker action")
        terms_value = payload.get("terms")
        if not isinstance(terms_value, (list, tuple)):
            raise TypeError("Worker search terms must be an array")
        terms = tuple(
            dict.fromkeys(
                str(term).strip() for term in terms_value if str(term).strip()
            )
        )
        if not terms or len(terms) > 20 or any(len(term) > 200 for term in terms):
            raise ValueError("Worker search terms are invalid")
        max_items = int(payload.get("max_items") or 0)
        max_requests = int(payload.get("max_requests") or 0)
        deadline = float(payload.get("deadline_seconds") or 0)
        if not 1 <= max_items <= 10_000:
            raise ValueError("Worker item budget is invalid")
        if not 1 <= max_requests <= 1_000:
            raise ValueError("Worker request budget is invalid")
        if not 1 <= deadline <= 7_200:
            raise ValueError("Worker deadline is invalid")
        cursor = payload.get("initial_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("Worker cursor must be a string")
        return cls(
            source_id,
            terms,
            max_items,
            max_requests,
            deadline,
            cursor,
            _required_path(payload, "browser_executable"),
            _required_path(payload, "profile_root"),
            _required_path(payload, "pseudonym_key_ref"),
            _required_path(payload, "contract_root"),
        )


@dataclass(frozen=True, slots=True)
class TiebaTargetWorkerRequest:
    action: str
    operation: str
    target_url: str
    max_items: int
    max_requests: int
    deadline_seconds: float
    initial_cursor: str | None
    browser_executable: Path
    profile_root: Path
    pseudonym_key_ref: Path

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TiebaTargetWorkerRequest:
        action = str(payload.get("action") or "")
        operations = {
            "tieba_detail": ("fetch_detail", {TiebaTargetKind.THREAD}),
            "tieba_forum": ("scan_channel", {TiebaTargetKind.FORUM}),
            "tieba_creator": ("list_creator", {TiebaTargetKind.CREATOR}),
            "tieba_comments": ("list_comments", {TiebaTargetKind.THREAD}),
        }
        if action not in operations:
            raise ValueError("Unsupported CBCE Tieba target action")
        target_value = payload.get("target_url")
        if not isinstance(target_value, str) or len(target_value) > 2_048:
            raise ValueError("Worker Tieba target is invalid")
        parsed = parse_tieba_target(target_value)
        operation, allowed_kinds = operations[action]
        if parsed.kind not in allowed_kinds:
            raise ValueError("Worker Tieba target kind is invalid")
        max_items = int(payload.get("max_items") or 0)
        max_requests = int(payload.get("max_requests") or 0)
        deadline = float(payload.get("deadline_seconds") or 0)
        if not 1 <= max_items <= 10_000:
            raise ValueError("Worker item budget is invalid")
        if not 1 <= max_requests <= 1_000:
            raise ValueError("Worker request budget is invalid")
        if not 1 <= deadline <= 7_200:
            raise ValueError("Worker deadline is invalid")
        cursor = payload.get("initial_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("Worker cursor must be a string")
        if action == "tieba_detail" and cursor is not None:
            raise ValueError("Tieba detail does not accept a cursor")
        return cls(
            action,
            operation,
            parsed.canonical_url,
            max_items,
            max_requests,
            deadline,
            cursor,
            _required_path(payload, "browser_executable"),
            _required_path(payload, "profile_root"),
            _required_path(payload, "pseudonym_key_ref"),
        )


@dataclass(frozen=True, slots=True)
class BilibiliDetailWorkerRequest:
    target_url: str
    deadline_seconds: float
    browser_executable: Path
    profile_root: Path
    pseudonym_key_ref: Path

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BilibiliDetailWorkerRequest:
        if payload.get("action") != "bilibili_detail":
            raise ValueError("Unsupported CBCE worker action")
        target = payload.get("target_url")
        if not isinstance(target, str) or not target.strip() or len(target) > 2_048:
            raise ValueError("Worker detail target is invalid")
        parsed = parse_bilibili_target(target)
        if parsed.kind is not BilibiliTargetKind.VIDEO:
            raise ValueError("Worker detail target must be a Bilibili video")
        deadline = float(payload.get("deadline_seconds") or 0)
        if not 1 <= deadline <= 7_200:
            raise ValueError("Worker deadline is invalid")
        return cls(
            parsed.canonical_url,
            deadline,
            _required_path(payload, "browser_executable"),
            _required_path(payload, "profile_root"),
            _required_path(payload, "pseudonym_key_ref"),
        )


@dataclass(frozen=True, slots=True)
class BilibiliPagedTargetWorkerRequest:
    action: str
    operation: str
    target_url: str
    root_comment_id: str | None
    max_items: int
    max_requests: int
    deadline_seconds: float
    initial_cursor: str | None
    browser_executable: Path
    profile_root: Path
    pseudonym_key_ref: Path

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> BilibiliPagedTargetWorkerRequest:
        action = payload.get("action")
        operation_by_action = {
            "bilibili_creator": "list_creator",
            "bilibili_comments": "list_comments",
            "bilibili_child_comments": "list_child_comments",
        }
        if action not in operation_by_action:
            raise ValueError("Unsupported CBCE paged-target worker action")
        target = payload.get("target_url")
        if not isinstance(target, str) or not target.strip() or len(target) > 2_048:
            raise ValueError("Worker target is invalid")
        parsed = parse_bilibili_target(target)
        expected_kind = (
            BilibiliTargetKind.CREATOR
            if action == "bilibili_creator"
            else BilibiliTargetKind.VIDEO
        )
        if parsed.kind is not expected_kind:
            raise ValueError("Worker target has the wrong Bilibili kind")
        root_id = payload.get("root_comment_id")
        if action == "bilibili_child_comments":
            if not isinstance(root_id, str) or not root_id.isdigit() or root_id == "0":
                raise ValueError("Worker root comment identity is invalid")
        elif root_id is not None:
            raise ValueError("Worker root comment identity is not accepted")
        max_items = int(payload.get("max_items") or 0)
        max_requests = int(payload.get("max_requests") or 0)
        deadline = float(payload.get("deadline_seconds") or 0)
        if not 1 <= max_items <= 10_000:
            raise ValueError("Worker item budget is invalid")
        if not 1 <= max_requests <= 1_000:
            raise ValueError("Worker request budget is invalid")
        if not 1 <= deadline <= 7_200:
            raise ValueError("Worker deadline is invalid")
        cursor = payload.get("initial_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("Worker cursor must be a string")
        return cls(
            str(action),
            operation_by_action[str(action)],
            parsed.canonical_url,
            root_id,
            max_items,
            max_requests,
            deadline,
            cursor,
            _required_path(payload, "browser_executable"),
            _required_path(payload, "profile_root"),
            _required_path(payload, "pseudonym_key_ref"),
        )


class WorkerWriter:
    def __init__(self, start: WorkerEnvelope) -> None:
        self.start = start
        self.sequence = 0

    def emit(
        self, kind: WorkerMessageKind, payload: Mapping[str, Any] | None = None
    ) -> None:
        self.sequence += 1
        message = WorkerEnvelope(
            kind=kind,
            sequence=self.sequence,
            run_id=self.start.run_id,
            source_run_id=self.start.source_run_id,
            source_id=self.start.source_id,
            provider_id=self.start.provider_id,
            operation=self.start.operation,
            payload=payload or {},
        )
        sys.stdout.buffer.write(encode_message(message))
        sys.stdout.buffer.flush()


async def run_bilibili_worker(
    start: WorkerEnvelope,
    request: BilibiliWorkerRequest,
    cancellation: CancellationToken,
    writer: WorkerWriter,
) -> None:
    if start.source_id != "bilibili" or start.provider_id != "cbce_bilibili":
        raise ValueError("Worker identity does not match the Bilibili provider")
    profile = ProfileNamespace(request.profile_root, SOURCE_REGISTRY).profile(
        "bilibili", "default"
    )
    key = PseudonymKeyStore.load_reference(request.pseudonym_key_ref)
    browser = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(request.browser_executable, profile),
        owner_id=str(start.source_run_id),
    )
    adapter = BilibiliSearchAdapter(
        BilibiliDomSearchProvider(browser), IdentityPseudonymizer(key)
    )
    context = RunContext(
        run_id=str(start.run_id),
        keyword_id=0,
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="search",
        target={"kind": "keyword"},
        terms=request.terms,
        filters={},
        budgets=RunBudgets(
            max_items=request.max_items,
            max_requests=request.max_requests,
            deadline_seconds=request.deadline_seconds,
        ),
    )
    await run_search_adapter_worker(
        adapter,
        context,
        cancellation,
        writer,
        initial_cursor=request.initial_cursor,
        page_size=20,
    )


async def run_search_adapter_worker(
    adapter: SearchAdapter,
    context: RunContext,
    cancellation: CancellationToken,
    writer: WorkerWriter,
    *,
    initial_cursor: object | None,
    page_size: int,
) -> None:
    """Run any bounded search adapter using the shared worker event contract."""

    if adapter.source_id != context.source_id or adapter.provider_id != context.provider_id:
        raise ValueError("Search adapter identity does not match the run context")
    if context.operation != "search":
        raise ValueError("Shared search worker requires the search operation")
    if page_size <= 0:
        raise ValueError("Search page size must be positive")
    writer.emit(WorkerMessageKind.RUN_STARTED)
    writer.emit(WorkerMessageKind.BROWSER_OPENING)
    opened = False
    item_count = 0
    request_count = 0
    last_cursor = initial_cursor
    completed = False
    try:
        opened = True
        await adapter.open(context, cancellation)

        async def fetch(cursor: object | None, limit: int):
            return await adapter.fetch_page(context, cursor, limit)

        async for batch in bounded_pages(
            fetch,
            budgets=context.budgets,
            cancellation=cancellation,
            initial_cursor=initial_cursor,
            page_size=min(page_size, context.budgets.max_items),
        ):
            request_count = batch.request_number
            for record in batch.items:
                cancellation.raise_if_cancelled()
                writer.emit(
                    WorkerMessageKind.ITEM,
                    _record_payload(record),
                )
                item_count += 1
            writer.emit(
                WorkerMessageKind.CHECKPOINT,
                {"cursor": batch.next_cursor},
            )
            last_cursor = batch.next_cursor
        completed = True
    finally:
        if opened:
            await adapter.close()
    if completed:
        writer.emit(
            WorkerMessageKind.COMPLETE,
            {
                "item_count": item_count,
                "request_count": request_count,
                "cursor": last_cursor,
            },
        )


async def run_tieba_worker(
    start: WorkerEnvelope,
    request: TiebaWorkerRequest,
    cancellation: CancellationToken,
    writer: WorkerWriter,
) -> None:
    if (
        start.source_id != "tieba"
        or start.provider_id != "cbce_tieba"
        or start.operation != "search"
    ):
        raise ValueError("Worker identity does not match the Tieba provider")
    profile = ProfileNamespace(request.profile_root, SOURCE_REGISTRY).profile(
        "tieba", "default"
    )
    key = PseudonymKeyStore.load_reference(request.pseudonym_key_ref)
    browser = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(request.browser_executable, profile),
        owner_id=str(start.source_run_id),
    )
    owned = OwnedBrowserPage(
        browser,
        NavigationPolicy(
            "tieba",
            frozenset({"tieba.baidu.com"}),
            frozenset({"passport.baidu.com"}),
        ),
    )
    provider = TiebaDomSearchProvider(
        owned,
        TIEBA_SEARCH_DOM_CONTRACT,
        on_auth_required=lambda: writer.emit(
            WorkerMessageKind.AUTH_REQUIRED,
            {"message": "Complete Tieba login in the visible browser."},
        ),
        on_authenticated=lambda: writer.emit(
            WorkerMessageKind.AUTHENTICATED,
            {"message": "Tieba browser session is authenticated."},
        ),
    )
    adapter = TiebaSearchAdapter(provider, IdentityPseudonymizer(key))
    context = RunContext(
        run_id=str(start.run_id),
        keyword_id=0,
        source_id="tieba",
        provider_id="cbce_tieba",
        operation="search",
        target={"kind": "keyword"},
        terms=request.terms,
        filters={"coverage": "partial_dom"},
        budgets=RunBudgets(
            max_items=request.max_items,
            max_requests=request.max_requests,
            deadline_seconds=request.deadline_seconds,
        ),
    )
    await run_search_adapter_worker(
        adapter,
        context,
        cancellation,
        writer,
        initial_cursor=request.initial_cursor,
        page_size=4,
    )


async def run_observed_dom_worker(
    start: WorkerEnvelope,
    request: ObservedDomWorkerRequest,
    cancellation: CancellationToken,
    writer: WorkerWriter,
) -> None:
    runtime_spec = OBSERVED_DOM_RUNTIME_SPECS[request.source_id]
    if (
        start.source_id != runtime_spec.source_id
        or start.provider_id != runtime_spec.provider_id
        or start.operation != "search"
    ):
        raise ValueError("Worker identity does not match reviewed DOM provider")
    contract = load_observed_dom_contract(
        request.contract_root,
        request.source_id,
    )
    profile = ProfileNamespace(request.profile_root, SOURCE_REGISTRY).profile(
        request.source_id, "default"
    )
    key = PseudonymKeyStore.load_reference(request.pseudonym_key_ref)
    browser = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(request.browser_executable, profile),
        owner_id=str(start.source_run_id),
    )
    owned = OwnedBrowserPage(
        browser,
        NavigationPolicy(
            runtime_spec.source_id,
            runtime_spec.allowed_hosts,
            runtime_spec.login_hosts,
            login_selectors=contract.login_selectors,
            login_detection_grace_ms=2_000,
        ),
    )
    auth_required = lambda: writer.emit(
        WorkerMessageKind.AUTH_REQUIRED,
        {"message": f"Complete {runtime_spec.label} login in the visible browser."},
    )
    authenticated = lambda: writer.emit(
        WorkerMessageKind.AUTHENTICATED,
        {"message": f"{runtime_spec.label} browser session is authenticated."},
    )
    pseudonymizer = IdentityPseudonymizer(key)
    if runtime_spec.mode == "weibo_post_v1":
        if not isinstance(contract.selectors, WeiboDomContract):
            raise ValueError("Reviewed Weibo contract has the wrong selector mode")
        provider = WeiboDomSearchProvider(
            owned,
            contract.selectors,
            build_search_url=contract.build_search_url,
            on_auth_required=auth_required,
            on_authenticated=authenticated,
        )
        adapter: SearchAdapter = WeiboSearchAdapter(provider, pseudonymizer)
    else:
        if not isinstance(contract.selectors, BrowserVideoDomContract):
            raise ValueError("Reviewed DOM contract has the wrong selector mode")
        provider = BrowserVideoDomSearchProvider(
            source_id=runtime_spec.source_id,
            provider_id=runtime_spec.provider_id,
            content_kinds=runtime_spec.content_kinds,
            browser_page=owned,
            contract=contract.selectors,
            build_search_url=contract.build_search_url,
            canonicalize=runtime_spec.canonicalize,
            media_hosts=runtime_spec.media_hosts,
            metric_ids=frozenset(contract.selectors.metric_selectors),
            identity_builder=runtime_spec.identity_builder,
            on_auth_required=auth_required,
            on_authenticated=authenticated,
        )
        adapter = BrowserVideoSearchAdapter(
            source_id=runtime_spec.source_id,
            provider_id=runtime_spec.provider_id,
            provider=provider,
            pseudonymizer=pseudonymizer,
            canonicalize=runtime_spec.canonicalize,
            content_kinds=runtime_spec.content_kinds,
            metric_ids=runtime_spec.metric_ids,
            identity_builder=runtime_spec.identity_builder,
        )
    context = RunContext(
        run_id=str(start.run_id),
        keyword_id=0,
        source_id=runtime_spec.source_id,
        provider_id=runtime_spec.provider_id,
        operation="search",
        target={"kind": "keyword"},
        terms=request.terms,
        filters={
            "coverage": "reviewed_dom_contract",
            "contract_digest": contract.artifact_digest,
        },
        budgets=RunBudgets(
            max_items=request.max_items,
            max_requests=request.max_requests,
            deadline_seconds=request.deadline_seconds,
        ),
    )
    await run_search_adapter_worker(
        adapter,
        context,
        cancellation,
        writer,
        initial_cursor=request.initial_cursor,
        page_size=runtime_spec.page_size,
    )


async def run_tieba_target_worker(
    start: WorkerEnvelope,
    request: TiebaTargetWorkerRequest,
    cancellation: CancellationToken,
    writer: WorkerWriter,
) -> None:
    if (
        start.source_id != "tieba"
        or start.provider_id != "cbce_tieba"
        or start.operation != request.operation
    ):
        raise ValueError("Worker identity does not match the Tieba target provider")
    profile = ProfileNamespace(request.profile_root, SOURCE_REGISTRY).profile(
        "tieba", "default"
    )
    key = PseudonymKeyStore.load_reference(request.pseudonym_key_ref)
    browser = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(request.browser_executable, profile),
        owner_id=str(start.source_run_id),
    )
    owned = OwnedBrowserPage(
        browser,
        NavigationPolicy(
            "tieba",
            frozenset({"tieba.baidu.com"}),
            frozenset({"passport.baidu.com"}),
        ),
    )
    provider = TiebaDomTargetProvider(
        owned,
        forum_contract=TIEBA_FORUM_DOM_CONTRACT,
        creator_contract=TIEBA_CREATOR_DOM_CONTRACT,
        detail_contract=TIEBA_DETAIL_DOM_CONTRACT,
        on_auth_required=lambda: writer.emit(
            WorkerMessageKind.AUTH_REQUIRED,
            {"message": "Complete Tieba login in the visible browser."},
        ),
        on_authenticated=lambda: writer.emit(
            WorkerMessageKind.AUTHENTICATED,
            {"message": "Tieba browser session is authenticated."},
        ),
    )
    pseudonymizer = IdentityPseudonymizer(key)
    context = RunContext(
        run_id=str(start.run_id),
        keyword_id=0,
        source_id="tieba",
        provider_id="cbce_tieba",
        operation=request.operation,
        target={"kind": "content_url", "url": request.target_url},
        terms=(),
        filters={"coverage": "partial_dom"},
        budgets=RunBudgets(
            max_items=request.max_items,
            max_requests=request.max_requests,
            deadline_seconds=request.deadline_seconds,
            max_root_comments=(
                request.max_items if request.operation == "list_comments" else 0
            ),
            max_total_comments=(
                request.max_items if request.operation == "list_comments" else 0
            ),
        ),
    )
    if request.operation == "fetch_detail":
        adapter = TiebaDetailAdapter(provider, pseudonymizer)
        writer.emit(WorkerMessageKind.RUN_STARTED)
        writer.emit(WorkerMessageKind.BROWSER_OPENING)
        opened = False
        record = None
        try:
            opened = True
            await adapter.open(context, cancellation)
            record = await adapter.fetch()
            writer.emit(WorkerMessageKind.ITEM, _record_payload(record))
        finally:
            if opened:
                await adapter.close()
        writer.emit(
            WorkerMessageKind.COMPLETE,
            {"item_count": int(record is not None), "request_count": 1},
        )
        return
    if request.operation == "list_comments":
        comments_provider = TiebaDomCommentsProvider(owned)
        adapter = TiebaCommentsAdapter(comments_provider, pseudonymizer)
    else:
        adapter = TiebaTargetThreadsAdapter(provider, pseudonymizer)
    await run_paged_adapter_worker(
        adapter,
        context,
        cancellation,
        writer,
        initial_cursor=request.initial_cursor,
        page_size=min(20, request.max_items),
    )


async def run_paged_adapter_worker(
    adapter: SearchAdapter,
    context: RunContext,
    cancellation: CancellationToken,
    writer: WorkerWriter,
    *,
    initial_cursor: object | None,
    page_size: int,
) -> None:
    if adapter.source_id != context.source_id or adapter.provider_id != context.provider_id:
        raise ValueError("Paged adapter identity does not match the run context")
    if page_size <= 0:
        raise ValueError("Paged adapter page size must be positive")
    writer.emit(WorkerMessageKind.RUN_STARTED)
    writer.emit(WorkerMessageKind.BROWSER_OPENING)
    opened = False
    item_count = 0
    request_count = 0
    last_cursor = initial_cursor
    completed = False
    try:
        opened = True
        await adapter.open(context, cancellation)

        async def fetch(cursor: object | None, limit: int):
            return await adapter.fetch_page(context, cursor, limit)

        async for batch in bounded_pages(
            fetch,
            budgets=context.budgets,
            cancellation=cancellation,
            initial_cursor=initial_cursor,
            page_size=min(page_size, context.budgets.max_items),
        ):
            request_count = batch.request_number
            for record in batch.items:
                cancellation.raise_if_cancelled()
                writer.emit(WorkerMessageKind.ITEM, _record_payload(record))
                item_count += 1
            writer.emit(WorkerMessageKind.CHECKPOINT, {"cursor": batch.next_cursor})
            last_cursor = batch.next_cursor
        completed = True
    finally:
        if opened:
            await adapter.close()
    if completed:
        writer.emit(
            WorkerMessageKind.COMPLETE,
            {
                "item_count": item_count,
                "request_count": request_count,
                "cursor": last_cursor,
            },
        )


async def run_bilibili_detail_worker(
    start: WorkerEnvelope,
    request: BilibiliDetailWorkerRequest,
    cancellation: CancellationToken,
    writer: WorkerWriter,
) -> None:
    if (
        start.source_id != "bilibili"
        or start.provider_id != "cbce_bilibili"
        or start.operation != "fetch_detail"
    ):
        raise ValueError("Worker identity does not match the Bilibili detail provider")
    profile = ProfileNamespace(request.profile_root, SOURCE_REGISTRY).profile(
        "bilibili", "default"
    )
    key = PseudonymKeyStore.load_reference(request.pseudonym_key_ref)
    browser = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(request.browser_executable, profile),
        owner_id=str(start.source_run_id),
    )
    adapter = BilibiliDetailAdapter(
        BilibiliDomDetailProvider(browser), IdentityPseudonymizer(key)
    )
    context = RunContext(
        run_id=str(start.run_id),
        keyword_id=0,
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="fetch_detail",
        target={"kind": "content_url", "url": request.target_url},
        terms=(),
        filters={},
        budgets=RunBudgets(
            max_items=1,
            max_requests=1,
            deadline_seconds=request.deadline_seconds,
        ),
    )
    writer.emit(WorkerMessageKind.RUN_STARTED)
    writer.emit(WorkerMessageKind.BROWSER_OPENING)
    opened = False
    record = None
    try:
        opened = True
        await adapter.open(context, cancellation)
        cancellation.raise_if_cancelled()
        record = await adapter.fetch()
        cancellation.raise_if_cancelled()
        writer.emit(WorkerMessageKind.ITEM, _record_payload(record))
    finally:
        if opened:
            await adapter.close()
    if record is not None:
        writer.emit(
            WorkerMessageKind.COMPLETE,
            {"item_count": 1, "request_count": 1},
        )


async def run_bilibili_paged_target_worker(
    start: WorkerEnvelope,
    request: BilibiliPagedTargetWorkerRequest,
    cancellation: CancellationToken,
    writer: WorkerWriter,
) -> None:
    if (
        start.source_id != "bilibili"
        or start.provider_id != "cbce_bilibili"
        or start.operation != request.operation
    ):
        raise ValueError("Worker identity does not match the Bilibili provider")
    profile = ProfileNamespace(request.profile_root, SOURCE_REGISTRY).profile(
        "bilibili", "default"
    )
    key = PseudonymKeyStore.load_reference(request.pseudonym_key_ref)
    browser = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(request.browser_executable, profile),
        owner_id=str(start.source_run_id),
    )
    pseudonymizer = IdentityPseudonymizer(key)
    target: dict[str, str] = {
        "kind": "creator_url" if request.operation == "list_creator" else "content_url",
        "url": request.target_url,
    }
    if request.operation == "list_creator":
        adapter: Any = BilibiliCreatorAdapter(
            BilibiliDomCreatorProvider(browser), pseudonymizer
        )
    elif request.operation == "list_child_comments":
        assert request.root_comment_id is not None
        target["root_comment_id"] = request.root_comment_id
        adapter = BilibiliChildCommentsAdapter(
            BilibiliDomCommentsProvider(
                browser,
                on_auth_required=lambda: writer.emit(
                    WorkerMessageKind.AUTH_REQUIRED,
                    {"message": "Complete Bilibili login in the visible browser."},
                ),
                on_authenticated=lambda: writer.emit(
                    WorkerMessageKind.AUTHENTICATED,
                    {"message": "Bilibili browser session is authenticated."},
                ),
            ),
            pseudonymizer,
        )
    else:
        adapter = BilibiliCommentsAdapter(
            BilibiliDomCommentsProvider(
                browser,
                on_auth_required=lambda: writer.emit(
                    WorkerMessageKind.AUTH_REQUIRED,
                    {"message": "Complete Bilibili login in the visible browser."},
                ),
                on_authenticated=lambda: writer.emit(
                    WorkerMessageKind.AUTHENTICATED,
                    {"message": "Bilibili browser session is authenticated."},
                ),
            ),
            pseudonymizer,
        )
    comment_budget = request.max_items if "comments" in request.operation else 0
    context = RunContext(
        run_id=str(start.run_id),
        keyword_id=0,
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation=request.operation,
        target=target,
        terms=(),
        filters={},
        budgets=RunBudgets(
            max_items=request.max_items,
            max_requests=request.max_requests,
            deadline_seconds=request.deadline_seconds,
            max_root_comments=(
                comment_budget if request.operation == "list_comments" else 0
            ),
            max_children_per_root=(
                comment_budget
                if request.operation == "list_child_comments"
                else 0
            ),
            max_total_comments=comment_budget,
        ),
    )
    writer.emit(WorkerMessageKind.RUN_STARTED)
    writer.emit(WorkerMessageKind.BROWSER_OPENING)
    opened = False
    item_count = 0
    request_count = 0
    last_cursor = request.initial_cursor
    completed = False
    try:
        opened = True
        await adapter.open(context, cancellation)

        async def fetch(cursor: object | None, limit: int):
            return await adapter.fetch_page(context, cursor, limit)

        async for batch in bounded_pages(
            fetch,
            budgets=context.budgets,
            cancellation=cancellation,
            initial_cursor=request.initial_cursor,
            page_size=min(20, request.max_items),
        ):
            request_count = batch.request_number
            for record in batch.items:
                cancellation.raise_if_cancelled()
                writer.emit(WorkerMessageKind.ITEM, _record_payload(record))
                item_count += 1
            writer.emit(WorkerMessageKind.CHECKPOINT, {"cursor": batch.next_cursor})
            last_cursor = batch.next_cursor
        completed = True
    finally:
        if opened:
            await adapter.close()
    if completed:
        writer.emit(
            WorkerMessageKind.COMPLETE,
            {
                "item_count": item_count,
                "request_count": request_count,
                "cursor": last_cursor,
            },
        )


def main() -> int:
    ready = WorkerEnvelope(
        kind=WorkerMessageKind.READY,
        sequence=0,
        run_id=None,
        source_run_id=None,
        source_id=None,
        provider_id=None,
        operation=None,
    )
    sys.stdout.buffer.write(encode_message(ready))
    sys.stdout.buffer.flush()
    try:
        start = decode_message(sys.stdin.buffer.readline())
        if start.kind is not WorkerMessageKind.START:
            raise ValueError("Worker expected a start message")
        request = _request_from_payload(start.payload)
        return asyncio.run(_execute(start, request))
    except Exception as exc:
        if "start" in locals() and start.kind is WorkerMessageKind.START:
            failure = CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "CBCE worker failed before completing the operation.",
            )
            WorkerWriter(start).emit(WorkerMessageKind.ERROR, failure.as_event_error())
        sys.stderr.write(safe_diagnostic(f"{type(exc).__name__}: {exc}") + "\n")
        return 2


async def _execute(
    start: WorkerEnvelope,
    request: (
        BilibiliWorkerRequest
        | ObservedDomWorkerRequest
        | TiebaWorkerRequest
        | TiebaTargetWorkerRequest
        | BilibiliDetailWorkerRequest
        | BilibiliPagedTargetWorkerRequest
    ),
) -> int:
    cancellation = CancellationToken()
    writer = WorkerWriter(start)
    _start_control_reader(start, cancellation, asyncio.get_running_loop())
    try:
        if isinstance(request, ObservedDomWorkerRequest):
            await run_observed_dom_worker(start, request, cancellation, writer)
        elif isinstance(request, TiebaTargetWorkerRequest):
            await run_tieba_target_worker(start, request, cancellation, writer)
        elif isinstance(request, TiebaWorkerRequest):
            await run_tieba_worker(start, request, cancellation, writer)
        elif isinstance(request, BilibiliPagedTargetWorkerRequest):
            await run_bilibili_paged_target_worker(
                start, request, cancellation, writer
            )
        elif isinstance(request, BilibiliDetailWorkerRequest):
            await run_bilibili_detail_worker(start, request, cancellation, writer)
        else:
            await run_bilibili_worker(start, request, cancellation, writer)
        return 0
    except CrawlerFailure as exc:
        _emit_terminal_error(writer, exc)
        return 2
    except Exception as exc:
        failure = CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "CBCE worker failed before completing the operation.",
        )
        _emit_terminal_error(writer, failure)
        sys.stderr.write(safe_diagnostic(f"{type(exc).__name__}: {exc}") + "\n")
        return 2


def _start_control_reader(
    start: WorkerEnvelope,
    cancellation: CancellationToken,
    loop: asyncio.AbstractEventLoop,
) -> None:
    def reader() -> None:
        pending = bytearray()
        descriptor = sys.stdin.fileno()
        while chunk := os.read(descriptor, 4096):
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, remainder = pending.partition(b"\n")
                pending[:] = remainder
                try:
                    message = decode_message(bytes(raw) + b"\n")
                except ValueError:
                    continue
                if message.run_id != start.run_id:
                    continue
                if message.kind in {
                    WorkerMessageKind.CANCEL,
                    WorkerMessageKind.SHUTDOWN,
                }:
                    loop.call_soon_threadsafe(cancellation.cancel)
                    return

    thread = threading.Thread(target=reader, name="cbce-worker-control", daemon=True)
    thread.start()


def _emit_terminal_error(writer: WorkerWriter, failure: CrawlerFailure) -> None:
    kind = (
        WorkerMessageKind.CANCELLED
        if failure.code is CrawlerErrorCode.CANCELLED
        else WorkerMessageKind.ERROR
    )
    writer.emit(kind, failure.as_event_error())


def _required_path(payload: Mapping[str, Any], key: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 1_024:
        raise ValueError(f"Worker {key} is invalid")
    return Path(value).expanduser().resolve()


def _request_from_payload(
    payload: Mapping[str, Any],
) -> (
        BilibiliWorkerRequest
        | ObservedDomWorkerRequest
        | TiebaWorkerRequest
    | TiebaTargetWorkerRequest
    | BilibiliDetailWorkerRequest
    | BilibiliPagedTargetWorkerRequest
):
    action = str(payload.get("action") or "")
    if (
        action.endswith("_search")
        and action.removesuffix("_search") in OBSERVED_DOM_RUNTIME_SPECS
    ):
        return ObservedDomWorkerRequest.from_payload(payload)
    if payload.get("action") in {
        "tieba_detail",
        "tieba_forum",
        "tieba_creator",
        "tieba_comments",
    }:
        return TiebaTargetWorkerRequest.from_payload(payload)
    if payload.get("action") == "tieba_search":
        return TiebaWorkerRequest.from_payload(payload)
    if payload.get("action") in {
        "bilibili_creator",
        "bilibili_comments",
        "bilibili_child_comments",
    }:
        return BilibiliPagedTargetWorkerRequest.from_payload(payload)
    if payload.get("action") == "bilibili_detail":
        return BilibiliDetailWorkerRequest.from_payload(payload)
    return BilibiliWorkerRequest.from_payload(payload)


def _record_payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "content_external_id"):
        return {
            "external_id": record.external_id,
            "content_external_id": record.content_external_id,
            "body": record.body,
            "author_pseudonym": record.author_pseudonym,
            "published_at": (
                record.published_at.isoformat()
                if record.published_at is not None
                else None
            ),
            "like_count": record.like_count,
            "child_count": record.child_count,
            "parent_external_id": record.parent_external_id,
            "root_external_id": record.root_external_id,
            "provenance": dict(record.provenance),
        }
    return {
        "external_id": record.external_id,
        "canonical_url": record.canonical_url,
        "title": record.title,
        "body": record.body,
        "author_pseudonym": record.author_pseudonym,
        "published_at": (
            record.published_at.isoformat() if record.published_at is not None else None
        ),
        "metrics": dict(record.metrics),
        "media": [dict(item) for item in record.media],
        "provenance": dict(record.provenance),
    }


if __name__ == "__main__":
    raise SystemExit(main())
