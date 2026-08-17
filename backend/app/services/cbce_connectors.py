"""Compatibility connectors backed by isolated CBCE workers."""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..crawlers.licensed.mediacrawler.policy import (
    LicensedReuseError,
    assert_licensed_reuse_allowed,
)
from ..crawlers.licensed.mediacrawler.source_map import load_source_map
from ..crawlers.observed_dom_contract import (
    DomContractUnavailable,
    load_observed_dom_contract,
)
from ..crawlers.observed_dom_runtime import OBSERVED_DOM_RUNTIME_SPECS
from ..crawlers.runtime import (
    BoundedMessageBuffer,
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    PseudonymKeyStore,
    RunBudgets,
    WorkerEnvelope,
    WorkerMessageKind,
    WorkerProcessSpec,
    WorkerProcessSupervisor,
    safe_worker_environment,
)
from .cbce_runtime import cbce_browser_preflight
from .connectors import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)


@dataclass(frozen=True, slots=True)
class CbceSearchSpec:
    source_id: str
    label: str
    provider_id: str
    action: str
    interaction_fields: tuple[str, ...]
    coverage: str


@dataclass(frozen=True, slots=True)
class BrowserCommentScan:
    records: tuple[CommentRecord, ...]
    request_count: int
    root_count: int
    child_count: int
    truncated: bool
    provider_id: str
    pagination_truncated: bool = False


class CbceWorkerSearchConnector(SourceConnector):
    group = "Content Bot clean-room browser"

    def __init__(
        self,
        spec: CbceSearchSpec,
        process_supervisor: WorkerProcessSupervisor | None = None,
    ) -> None:
        self.spec = spec
        self.source_id = spec.source_id
        self.label = spec.label
        self.capabilities = ConnectorCapabilities(
            global_search=True,
            requires_login=True,
            interaction_fields=spec.interaction_fields,
        )
        self.process_supervisor = process_supervisor or WorkerProcessSupervisor()

    @property
    def configured(self) -> bool:
        return bool(
            settings.content_bot_cbce_enabled and cbce_browser_preflight()["ready"]
        )

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.content_bot_cbce_enabled:
            return ConnectorStatus(
                "disabled",
                f"CBCE {self.label} remains behind CONTENT_BOT_CBCE_ENABLED.",
            )
        preflight = cbce_browser_preflight()
        if not preflight["ready"]:
            return ConnectorStatus(
                "setup_required",
                f"CBCE browser preflight failed: {preflight['reason_code']}",
            )
        return ConnectorStatus(
            "ready",
            f"CBCE {self.label} DOM worker is locally ready; coverage is {self.spec.coverage}.",
        )

    def _extra_worker_payload(self) -> dict[str, Any]:
        return {}

    def _worker_budgets(self, query: SearchQuery) -> tuple[int, int]:
        return (
            query.max_items,
            query.request_limit(query.max_items, maximum=1_000),
        )

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        del checkpoint
        if not self.configured:
            raise RuntimeError(f"CBCE {self.label} browser preflight is not ready")
        backend_root = Path(__file__).resolve().parents[2]
        executable = (
            (
                settings.content_bot_cbce_browser_executable_path
                or settings.content_bot_coccoc_executable_path
            )
            .expanduser()
            .resolve()
        )
        key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
        key_store.load_or_create()
        initial_cursor = query.resume_cursor("provider", stream="search")
        max_items, max_requests = self._worker_budgets(query)
        if max_items < query.max_items and query.warning_callback:
            await query.warning_callback(
                "PROVIDER_BUDGET_CAPPED",
                f"{self.label} is limited to {max_items} items per run.",
            )
        run_id = f"cbce-{self.source_id}-{uuid.uuid4().hex}"
        start = WorkerEnvelope(
            kind=WorkerMessageKind.START,
            sequence=0,
            run_id=run_id,
            source_run_id=run_id,
            source_id=self.source_id,
            provider_id=self.spec.provider_id,
            operation="search",
            payload={
                "action": self.spec.action,
                "terms": query.search_terms,
                "max_items": max_items,
                "max_requests": max_requests,
                "deadline_seconds": query.deadline_limit(
                    settings.mediacrawler_timeout_seconds
                ),
                "initial_cursor": initial_cursor,
                "browser_executable": str(executable),
                "profile_root": str(settings.content_bot_cbce_profile_root),
                "pseudonym_key_ref": str(key_store.path),
                **self._extra_worker_payload(),
            },
        )
        spec = WorkerProcessSpec(
            command=(sys.executable, "-m", "app.crawlers.worker"),
            cwd=backend_root,
            environment=safe_worker_environment(
                {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            ),
            ready_timeout_seconds=15,
            heartbeat_timeout_seconds=90,
            graceful_shutdown_seconds=10,
            hard_kill_seconds=5,
        )
        buffer = BoundedMessageBuffer(
            maxsize=settings.content_bot_cbce_event_queue_size
        )

        async def receive(message: WorkerEnvelope) -> None:
            await buffer.put(message)

        process_task = asyncio.create_task(
            self.process_supervisor.execute(
                spec,
                start,
                on_message=receive,
            )
        )
        try:
            while True:
                message = await _next_worker_message(buffer, process_task)
                if message.kind is WorkerMessageKind.BROWSER_OPENING:
                    if query.progress_callback:
                        await query.progress_callback(
                            "opening_browser",
                            f"CBCE opened an isolated visible {self.label} browser.",
                        )
                    continue
                if message.kind is WorkerMessageKind.AUTH_REQUIRED:
                    if query.progress_callback:
                        await query.progress_callback(
                            "waiting_for_login",
                            str(message.payload.get("message") or "Complete login in the visible browser."),
                        )
                    continue
                if message.kind is WorkerMessageKind.AUTHENTICATED:
                    if query.progress_callback:
                        await query.progress_callback(
                            "authenticated",
                            str(message.payload.get("message") or "Browser session is authenticated."),
                        )
                    continue
                if message.kind is WorkerMessageKind.ITEM:
                    yield _raw_item(
                        message.payload,
                        source_id=self.source_id,
                        provider_id=self.spec.provider_id,
                    )
                    continue
                if message.kind is WorkerMessageKind.CHECKPOINT:
                    query.report_cursor(
                        "provider", message.payload.get("cursor"), stream="search"
                    )
                    continue
                if message.kind is WorkerMessageKind.COMPLETE:
                    break
                if message.kind in {
                    WorkerMessageKind.CANCELLED,
                    WorkerMessageKind.ERROR,
                }:
                    raise _worker_failure(message)
            await process_task
        except BaseException:
            if not process_task.done():
                process_task.cancel()
            await asyncio.gather(process_task, return_exceptions=True)
            raise


class CbceBilibiliConnector(CbceWorkerSearchConnector):
    def __init__(
        self,
        process_supervisor: WorkerProcessSupervisor | None = None,
    ) -> None:
        super().__init__(
            CbceSearchSpec(
                "bilibili",
                "Bilibili",
                "cbce_bilibili",
                "bilibili_search",
                ("view_count",),
                "public_dom",
            ),
            process_supervisor,
        )

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 20,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[CommentRecord]:
        total = min(max(int(max_items), 1), 1_000)
        scan = await self.scan_comments(
            target_url,
            RunBudgets(
                max_items=total,
                max_requests=total,
                deadline_seconds=min(settings.mediacrawler_timeout_seconds, 7_200),
                max_root_comments=total,
                max_total_comments=total,
            ),
            initial_cursor=initial_cursor,
        )
        for record in scan.records:
            yield record

    async def scan_comments(
        self,
        target_url: str,
        budgets: RunBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
        initial_cursor: str | None = None,
    ) -> BrowserCommentScan:
        return await _scan_browser_comments(
            connector=self,
            action="bilibili_comments",
            operation="list_comments",
            target_url=target_url,
            budgets=budgets,
            sort=sort,
            cancellation=cancellation,
            initial_cursor=initial_cursor,
        )

    async def scan_child_comments(
        self,
        target_url: str,
        root_comment_id: str,
        budgets: RunBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
    ) -> BrowserCommentScan:
        root_id = str(root_comment_id or "").strip()
        if not root_id or len(root_id) > 256:
            raise ValueError("Bilibili root comment identity is invalid")
        return await _scan_browser_comments(
            connector=self,
            action="bilibili_child_comments",
            operation="list_child_comments",
            target_url=target_url,
            budgets=budgets,
            sort=sort,
            cancellation=cancellation,
            root_comment_id=root_id,
        )


class CbceObservedDomConnector(CbceWorkerSearchConnector):
    """Search connector enabled only by a reviewed project-owned DOM contract."""

    def __init__(
        self,
        source_id: str,
        process_supervisor: WorkerProcessSupervisor | None = None,
    ) -> None:
        try:
            runtime_spec = OBSERVED_DOM_RUNTIME_SPECS[source_id]
        except KeyError as exc:
            raise ValueError("Observed DOM source is unsupported") from exc
        self.runtime_spec = runtime_spec
        super().__init__(
            CbceSearchSpec(
                runtime_spec.source_id,
                runtime_spec.label,
                runtime_spec.provider_id,
                f"{runtime_spec.source_id}_search",
                tuple(sorted(runtime_spec.metric_ids)),
                "reviewed_dom_contract",
            ),
            process_supervisor,
        )

    @property
    def configured(self) -> bool:
        if not super().configured:
            return False
        try:
            load_observed_dom_contract(
                settings.content_bot_cbce_contract_root,
                self.source_id,
            )
        except DomContractUnavailable:
            return False
        return True

    async def healthcheck(self) -> ConnectorStatus:
        status = await super().healthcheck()
        if status.state != "ready":
            return status
        try:
            contract = load_observed_dom_contract(
                settings.content_bot_cbce_contract_root,
                self.source_id,
            )
        except DomContractUnavailable as exc:
            return ConnectorStatus(
                "setup_required",
                exc.safe_message,
                reason_code=exc.reason_code,
            )
        return ConnectorStatus(
            "ready",
            f"CBCE {self.label} reviewed DOM contract {contract.artifact_digest[:12]} is locally ready.",
        )

    def _extra_worker_payload(self) -> dict[str, Any]:
        return {"contract_root": str(settings.content_bot_cbce_contract_root)}


class CbceLicensedWeiboConnector(CbceWorkerSearchConnector):
    """Weibo search using the licensed upstream mobile API vocabulary.

    This is opt-in only.  The worker still owns the browser/profile and the
    parent still owns persistence, budgets and cancellation.
    """

    def __init__(
        self,
        process_supervisor: WorkerProcessSupervisor | None = None,
    ) -> None:
        super().__init__(
            CbceSearchSpec(
                "weibo",
                "Weibo (licensed provider)",
                "licensed_weibo",
                "licensed_weibo_search",
                ("like_count", "comment_count", "share_count"),
                "licensed_mobile_api",
            ),
            process_supervisor,
        )

    @property
    def configured(self) -> bool:
        if not settings.content_bot_cbce_enabled:
            return False
        try:
            assert_licensed_reuse_allowed(
                {
                    "non_commercial_learning": settings.content_bot_licensed_reuse_noncommercial_only,
                }
            )
            load_source_map(
                Path(__file__).resolve().parents[1]
                / "crawlers"
                / "licensed"
                / "mediacrawler"
            )
        except (LicensedReuseError, OSError, ValueError):
            return False
        return cbce_browser_preflight()["ready"]

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.content_bot_cbce_enabled:
            return ConnectorStatus("disabled", "CBCE is disabled.")
        try:
            assert_licensed_reuse_allowed(
                {
                    "non_commercial_learning": settings.content_bot_licensed_reuse_noncommercial_only,
                }
            )
            load_source_map(
                Path(__file__).resolve().parents[1]
                / "crawlers"
                / "licensed"
                / "mediacrawler"
            )
        except LicensedReuseError as exc:
            return ConnectorStatus(
                "disabled_by_policy",
                str(exc),
                reason_code="LICENSED_REUSE_POLICY",
            )
        except (OSError, ValueError) as exc:
            return ConnectorStatus(
                "setup_required",
                str(exc),
                reason_code="LICENSED_REUSE_PROVENANCE",
            )
        status = await super().healthcheck()
        if status.state != "ready":
            return status
        return ConnectorStatus(
            "ready",
            "Licensed Weibo mobile API facade is locally ready; live auth and rate behavior remain manual-canary gates.",
        )

    def _extra_worker_payload(self) -> dict[str, Any]:
        return {"contract_root": str(settings.content_bot_cbce_contract_root)}

    def _worker_budgets(self, query: SearchQuery) -> tuple[int, int]:
        policy = assert_licensed_reuse_allowed(
            {
                "non_commercial_learning": settings.content_bot_licensed_reuse_noncommercial_only,
            }
        )
        max_items = min(query.max_items, policy.max_items)
        return (
            max_items,
            query.request_limit(max_items, maximum=policy.max_requests),
        )


class CbceTiebaConnector(CbceWorkerSearchConnector):
    def __init__(
        self,
        process_supervisor: WorkerProcessSupervisor | None = None,
    ) -> None:
        super().__init__(
            CbceSearchSpec(
                "tieba",
                "Baidu Tieba",
                "cbce_tieba",
                "tieba_search",
                ("comment_count",),
                "partial_dom",
            ),
            process_supervisor,
        )

    async def fetch_detail(self, target_url: str) -> RawContentItem:
        items = [
            item
            async for item in self._target_operation(
                "tieba_detail", "fetch_detail", target_url, max_items=1
            )
        ]
        if len(items) != 1:
            raise RuntimeError("CBCE Tieba detail did not return exactly one item")
        return items[0]

    async def list_creator(
        self,
        target_url: str,
        *,
        max_items: int = 20,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[RawContentItem]:
        async for item in self._target_operation(
            "tieba_creator",
            "list_creator",
            target_url,
            max_items=max_items,
            initial_cursor=initial_cursor,
        ):
            yield item

    async def scan_channel(
        self,
        channel: dict[str, Any],
        query: SearchQuery,
    ) -> AsyncIterator[RawContentItem]:
        target_url = str(channel.get("normalized_url") or channel.get("url") or "")
        initial_cursor = query.resume_cursor(
            "provider", stream="scan_channel", target=target_url
        )
        async for item in self._target_operation(
            "tieba_forum",
            "scan_channel",
            target_url,
            max_items=query.max_items,
            initial_cursor=initial_cursor,
            query=query,
        ):
            yield item

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 20,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[CommentRecord]:
        total = min(max(int(max_items), 1), 1_000)
        scan = await self.scan_comments(
            target_url,
            RunBudgets(
                max_items=total,
                max_requests=total,
                deadline_seconds=min(settings.mediacrawler_timeout_seconds, 7_200),
                max_root_comments=total,
                max_total_comments=total,
            ),
            initial_cursor=initial_cursor,
        )
        for record in scan.records:
            yield record

    async def scan_comments(
        self,
        target_url: str,
        budgets: RunBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
        initial_cursor: str | None = None,
    ) -> BrowserCommentScan:
        return await _scan_browser_comments(
            connector=self,
            action="tieba_comments",
            operation="list_comments",
            target_url=target_url,
            budgets=budgets,
            sort=sort,
            cancellation=cancellation,
            initial_cursor=initial_cursor,
        )

    async def _target_operation(
        self,
        action: str,
        operation: str,
        target_url: str,
        *,
        max_items: int,
        initial_cursor: str | None = None,
        query: SearchQuery | None = None,
    ) -> AsyncIterator[RawContentItem]:
        if not self.configured:
            raise RuntimeError("CBCE Tieba browser preflight is not ready")
        async for message in _worker_operation_messages(
            connector=self,
            action=action,
            operation=operation,
            payload={
                "target_url": target_url,
                "max_items": max_items,
                "max_requests": (
                    query.request_limit(max_items, maximum=1_000)
                    if query
                    else min(max(max_items, 1), 1_000)
                ),
                "deadline_seconds": (
                    query.deadline_limit(settings.mediacrawler_timeout_seconds)
                    if query
                    else min(settings.mediacrawler_timeout_seconds, 7_200)
                ),
                "initial_cursor": initial_cursor,
            },
            progress_callback=query.progress_callback if query else None,
        ):
            if message.kind is WorkerMessageKind.ITEM:
                yield _raw_item(
                    message.payload,
                    source_id="tieba",
                    provider_id="cbce_tieba",
                )
            elif message.kind is WorkerMessageKind.CHECKPOINT and query is not None:
                query.report_cursor(
                    "provider",
                    message.payload.get("cursor"),
                    stream="scan_channel",
                    target=target_url,
                )


async def _worker_operation_messages(
    *,
    connector: CbceWorkerSearchConnector,
    action: str,
    operation: str,
    payload: dict[str, Any],
    progress_callback=None,
) -> AsyncIterator[WorkerEnvelope]:
    backend_root = Path(__file__).resolve().parents[2]
    executable = (
        settings.content_bot_cbce_browser_executable_path
        or settings.content_bot_coccoc_executable_path
    ).expanduser().resolve()
    key_store = PseudonymKeyStore(settings.data_dir / "cbce-secrets")
    key_store.load_or_create()
    run_id = f"cbce-{connector.source_id}-{uuid.uuid4().hex}"
    start = WorkerEnvelope(
        kind=WorkerMessageKind.START,
        sequence=0,
        run_id=run_id,
        source_run_id=run_id,
        source_id=connector.source_id,
        provider_id=connector.spec.provider_id,
        operation=operation,
        payload={
            "action": action,
            **payload,
            "browser_executable": str(executable),
            "profile_root": str(settings.content_bot_cbce_profile_root),
            "pseudonym_key_ref": str(key_store.path),
        },
    )
    spec = WorkerProcessSpec(
        command=(sys.executable, "-m", "app.crawlers.worker"),
        cwd=backend_root,
        environment=safe_worker_environment(
            {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        ),
        ready_timeout_seconds=15,
        heartbeat_timeout_seconds=90,
        graceful_shutdown_seconds=10,
        hard_kill_seconds=5,
    )
    buffer = BoundedMessageBuffer(maxsize=settings.content_bot_cbce_event_queue_size)

    async def receive(message: WorkerEnvelope) -> None:
        await buffer.put(message)

    process_task = asyncio.create_task(
        connector.process_supervisor.execute(spec, start, on_message=receive)
    )
    try:
        while True:
            message = await _next_worker_message(buffer, process_task)
            if message.kind is WorkerMessageKind.BROWSER_OPENING and progress_callback:
                await progress_callback(
                    "opening_browser",
                    f"CBCE opened an isolated visible {connector.label} browser.",
                )
            elif message.kind is WorkerMessageKind.AUTH_REQUIRED and progress_callback:
                await progress_callback(
                    "waiting_for_login",
                    str(message.payload.get("message") or "Complete visible login."),
                )
            elif message.kind is WorkerMessageKind.AUTHENTICATED and progress_callback:
                await progress_callback(
                    "authenticated",
                    str(message.payload.get("message") or "Browser authenticated."),
                )
            yield message
            if message.kind in {
                WorkerMessageKind.COMPLETE,
                WorkerMessageKind.CANCELLED,
                WorkerMessageKind.ERROR,
            }:
                break
        await process_task
    except BaseException:
        if not process_task.done():
            process_task.cancel()
        await asyncio.gather(process_task, return_exceptions=True)
        raise


async def _scan_browser_comments(
    *,
    connector: CbceWorkerSearchConnector,
    action: str,
    operation: str,
    target_url: str,
    budgets: RunBudgets,
    sort: str,
    cancellation: CancellationToken | None,
    initial_cursor: str | None = None,
    root_comment_id: str | None = None,
) -> BrowserCommentScan:
    if sort != "new":
        raise ValueError("Browser comment provider only supports document order")
    if not connector.configured:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Clean-room browser comment provider is not locally ready.",
        )
    token = cancellation or CancellationToken()
    token.raise_if_cancelled()
    root_budget = budgets.max_root_comments or budgets.max_items
    total_budget = budgets.max_total_comments or budgets.max_items
    item_budget = min(budgets.max_items, root_budget, total_budget)
    if operation == "list_child_comments":
        child_budget = budgets.max_children_per_root or budgets.max_items
        item_budget = min(budgets.max_items, child_budget, total_budget)
    records: list[CommentRecord] = []
    request_count = 0
    last_cursor: object | None = initial_cursor
    terminal_seen = False
    payload = {
        "target_url": target_url,
        "max_items": item_budget,
        "max_requests": min(budgets.max_requests, 1_000),
        "deadline_seconds": min(budgets.deadline_seconds, 7_200),
        "initial_cursor": initial_cursor,
    }
    if root_comment_id is not None:
        payload["root_comment_id"] = root_comment_id
    async for message in _worker_operation_messages(
        connector=connector,
        action=action,
        operation=operation,
        payload=payload,
    ):
        token.raise_if_cancelled()
        if message.kind is WorkerMessageKind.ITEM:
            records.append(
                _comment_record(
                    message.payload,
                    source_id=connector.source_id,
                    provider_id=connector.spec.provider_id,
                )
            )
        elif message.kind is WorkerMessageKind.CHECKPOINT:
            last_cursor = message.payload.get("cursor")
        elif message.kind is WorkerMessageKind.COMPLETE:
            terminal_seen = True
            request_count = _nonnegative_int(message.payload.get("request_count"))
            last_cursor = message.payload.get("cursor", last_cursor)
        elif message.kind in {WorkerMessageKind.ERROR, WorkerMessageKind.CANCELLED}:
            raise _worker_failure(message)
    if not terminal_seen:
        raise CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "Clean-room comment worker ended without completion.",
        )
    root_count = sum(record.parent_external_id is None for record in records)
    child_count = len(records) - root_count
    pagination_truncated = last_cursor is not None
    truncated = pagination_truncated
    if operation == "list_comments" and any(
        record.child_count > 0 for record in records
    ):
        truncated = True
    return BrowserCommentScan(
        records=tuple(records),
        request_count=request_count,
        root_count=root_count,
        child_count=child_count,
        truncated=truncated,
        provider_id=connector.spec.provider_id,
        pagination_truncated=pagination_truncated,
    )


async def _next_worker_message(
    buffer: BoundedMessageBuffer,
    process_task: asyncio.Task[object],
) -> WorkerEnvelope:
    """Wait for output without hanging if the worker exits before an event."""

    if buffer.size:
        return await buffer.get()
    if process_task.done():
        await process_task
        raise RuntimeError("CBCE worker exited without a terminal event")
    message_task = asyncio.create_task(buffer.get())
    done, _ = await asyncio.wait(
        {message_task, process_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if message_task in done:
        return message_task.result()
    message_task.cancel()
    await asyncio.gather(message_task, return_exceptions=True)
    await process_task
    raise RuntimeError("CBCE worker exited without a terminal event")


def _raw_item(
    payload: Any,
    *,
    source_id: str,
    provider_id: str,
) -> RawContentItem:
    if not isinstance(payload, dict) and not hasattr(payload, "get"):
        raise ValueError("CBCE worker item payload is invalid")
    external_id = str(payload.get("external_id") or "").strip()
    canonical_url = str(payload.get("canonical_url") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not external_id or not canonical_url or not title:
        raise ValueError("CBCE worker item identity is incomplete")
    published = payload.get("published_at")
    published_at = datetime.fromisoformat(str(published)) if published else None
    metrics_value = payload.get("metrics") or {}
    metrics = {
        str(key): int(value)
        for key, value in metrics_value.items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    return RawContentItem(
        external_id=external_id,
        canonical_url=canonical_url,
        title=title[:500],
        body_snippet=str(payload.get("body") or "")[:4_000],
        author=str(payload.get("author_pseudonym") or ""),
        published_at=published_at,
        metrics=metrics,
        raw_payload={
            "provider_id": provider_id,
            "contract_version": f"cbce.{source_id}.content.v1",
        },
    )


def _comment_record(
    payload: Any,
    *,
    source_id: str,
    provider_id: str,
) -> CommentRecord:
    if not isinstance(payload, dict) and not hasattr(payload, "get"):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Clean-room comment payload is invalid.",
        )
    external_id = str(payload.get("external_id") or "").strip()
    content_external_id = str(payload.get("content_external_id") or "").strip()
    if (
        not external_id
        or not content_external_id
        or max(len(external_id), len(content_external_id)) > 256
    ):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Clean-room comment identity is incomplete.",
        )
    parent = _optional_identity(payload.get("parent_external_id"))
    root = _optional_identity(payload.get("root_external_id"))
    if parent is None and root is None:
        root = external_id
    if parent == external_id:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Clean-room comment hierarchy is self-referential.",
        )
    published = payload.get("published_at")
    try:
        published_at = datetime.fromisoformat(str(published)) if published else None
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Clean-room comment timestamp is invalid.",
        ) from exc
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    provenance_value = payload.get("provenance")
    provenance = provenance_value if isinstance(provenance_value, dict) else {}
    coverage = str(provenance.get("coverage") or "partial_dom")[:100]
    contract = str(
        provenance.get("contract_version")
        or f"cbce.{source_id}.comment.v1"
    )[:100]
    return CommentRecord(
        source_id=source_id,
        external_id=external_id,
        content_external_id=content_external_id,
        body=str(payload.get("body") or "")[:4_000],
        author_pseudonym=str(payload.get("author_pseudonym") or "")[:128],
        published_at=published_at,
        like_count=_nonnegative_int(payload.get("like_count")),
        child_count=_nonnegative_int(payload.get("child_count")),
        parent_external_id=parent,
        root_external_id=root,
        provenance={
            "provider_id": provider_id,
            "contract_version": contract,
            "coverage": coverage,
        },
    )


def _worker_failure(message: WorkerEnvelope) -> CrawlerFailure:
    raw_code = str(message.payload.get("code") or "")
    try:
        code = CrawlerErrorCode(raw_code)
    except ValueError:
        code = (
            CrawlerErrorCode.CANCELLED
            if message.kind is WorkerMessageKind.CANCELLED
            else CrawlerErrorCode.TRANSPORT_ERROR
        )
    safe_message = str(
        message.payload.get("message")
        or "Clean-room comment worker failed."
    )[:500]
    retry_after = message.payload.get("retry_after_seconds")
    try:
        retry_after_seconds = (
            max(0.0, float(retry_after)) if retry_after is not None else None
        )
    except (TypeError, ValueError):
        retry_after_seconds = None
    return CrawlerFailure(
        code,
        safe_message,
        retryable=bool(message.payload.get("retryable")),
        retry_after_seconds=retry_after_seconds,
    )


def _optional_identity(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > 256:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Clean-room comment relation identity is invalid.",
        )
    return text


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
