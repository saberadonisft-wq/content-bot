"""Cancellation-safe execution and backpressured event delivery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .adapter import SearchAdapter
from .contracts import (
    CancellationToken,
    ContentRecord,
    CrawlerEvent,
    CrawlerEventType,
    RunContext,
)
from .errors import CrawlerErrorCode, CrawlerFailure
from .paginator import bounded_pages

PersistRecord = Callable[[ContentRecord], Awaitable[None]]
CommitCursor = Callable[[Any | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    fetched_count: int
    persisted_count: int
    request_count: int
    last_cursor: Any | None


class CrawlerSupervisor:
    def __init__(self, *, event_queue_size: int = 100, cleanup_timeout: float = 10) -> None:
        if event_queue_size <= 0 or cleanup_timeout <= 0:
            raise ValueError("Queue size and cleanup timeout must be positive")
        self._events: asyncio.Queue[CrawlerEvent] = asyncio.Queue(
            maxsize=event_queue_size
        )
        self.cleanup_timeout = cleanup_timeout

    @property
    def event_queue_size(self) -> int:
        return self._events.qsize()

    @property
    def event_queue_capacity(self) -> int:
        return self._events.maxsize

    async def next_event(self) -> CrawlerEvent:
        return await self._events.get()

    async def events(self) -> AsyncIterator[CrawlerEvent]:
        while True:
            event = await self.next_event()
            yield event
            if event.type in {
                CrawlerEventType.COMPLETED,
                CrawlerEventType.FAILED,
                CrawlerEventType.CANCELLED,
            }:
                return

    async def _emit(
        self,
        context: RunContext,
        event_type: CrawlerEventType,
        *,
        current: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._events.put(
            CrawlerEvent(
                type=event_type,
                run_id=context.run_id,
                source_id=context.source_id,
                provider_id=context.provider_id,
                operation=context.operation,
                progress_current=current,
                progress_total=context.budgets.max_items,
                payload=payload or {},
            )
        )

    async def run_search(
        self,
        context: RunContext,
        adapter: SearchAdapter,
        *,
        persist: PersistRecord,
        commit_cursor: CommitCursor,
        initial_cursor: Any | None = None,
        cancellation: CancellationToken | None = None,
        page_size: int = 50,
    ) -> SupervisorResult:
        if adapter.source_id != context.source_id or adapter.provider_id != context.provider_id:
            raise ValueError("Adapter identity does not match the run context")
        token = cancellation or CancellationToken()
        fetched = 0
        persisted = 0
        requests = 0
        last_cursor = initial_cursor
        opened = False
        failure: BaseException | None = None
        result: SupervisorResult | None = None
        try:
            await self._emit(
                context,
                CrawlerEventType.RUN_STARTED,
                payload={"owns_resources": adapter.owns_resources},
            )
            token.raise_if_cancelled()
            opened = True
            await adapter.open(context, token)

            async def fetch(cursor: Any | None, limit: int):
                return await adapter.fetch_page(context, cursor, limit)

            async for batch in bounded_pages(
                fetch,
                budgets=context.budgets,
                cancellation=token,
                initial_cursor=initial_cursor,
                page_size=page_size,
            ):
                requests = batch.request_number
                for record in batch.items:
                    token.raise_if_cancelled()
                    fetched += 1
                    try:
                        await persist(record)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise CrawlerFailure(
                            CrawlerErrorCode.STORAGE_ERROR,
                            "Content persistence failed; checkpoint was not advanced.",
                        ) from exc
                    persisted += 1
                    await self._emit(
                        context,
                        CrawlerEventType.ITEM,
                        current=persisted,
                        payload={"external_id": record.external_id},
                    )
                # This ordering is the transaction boundary: every record from
                # the page is durable before its provider cursor is committed.
                await commit_cursor(batch.next_cursor)
                last_cursor = batch.next_cursor
                await self._emit(
                    context,
                    CrawlerEventType.PAGE_SCANNED,
                    current=persisted,
                    payload={
                        "request_number": batch.request_number,
                        "item_count": len(batch.items),
                        "truncated": batch.truncated,
                    },
                )
            result = SupervisorResult(fetched, persisted, requests, last_cursor)
        except asyncio.CancelledError as exc:
            failure = exc
            token.cancel()
            await self._emit(
                context,
                CrawlerEventType.CANCELLED,
                current=persisted,
                payload={"code": CrawlerErrorCode.CANCELLED.value},
            )
            raise
        except CrawlerFailure as exc:
            failure = exc
            event_type = (
                CrawlerEventType.CANCELLED
                if exc.code is CrawlerErrorCode.CANCELLED
                else CrawlerEventType.FAILED
            )
            await self._emit(
                context,
                event_type,
                current=persisted,
                payload=exc.as_event_error(),
            )
            raise
        except Exception as exc:
            failure = exc
            wrapped = CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "Crawler provider request failed.",
                retryable=True,
            )
            await self._emit(
                context,
                CrawlerEventType.FAILED,
                current=persisted,
                payload=wrapped.as_event_error(),
            )
            raise wrapped from exc
        finally:
            if opened:
                try:
                    await asyncio.wait_for(
                        adapter.close(), timeout=self.cleanup_timeout
                    )
                except Exception as cleanup_exc:
                    if failure is None:
                        raise CrawlerFailure(
                            CrawlerErrorCode.TRANSPORT_ERROR,
                            "Crawler resource cleanup failed.",
                        ) from cleanup_exc
        if result is None:
            raise RuntimeError("Crawler supervisor ended without a result")
        await self._emit(
            context,
            CrawlerEventType.COMPLETED,
            current=persisted,
            payload={"request_count": requests},
        )
        return result
