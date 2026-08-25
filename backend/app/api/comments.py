"""Manual bounded comment scans and normalized comment reads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..crawlers import SOURCE_REGISTRY
from ..crawlers.adapters.bluesky import BlueskyCommentBudgets
from ..crawlers.adapters.mastodon import MastodonCommentBudgets
from ..crawlers.adapters.reddit import RedditCommentBudgets
from ..crawlers.adapters.youtube import YouTubeCommentBudgets
from ..crawlers.contracts import Operation
from ..crawlers.runtime import (
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    RunBudgets,
)
from ..mongo import PersistenceStore
from ..services.cbce_runtime import cbce_provider_rollout_status
from ..services.connectors import SourceConnector


class CommentScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_root_comments: int = Field(default=100, ge=1, le=500)
    max_children_per_root: int = Field(default=100, ge=0, le=500)
    max_total_comments: int = Field(default=300, ge=1, le=1_000)
    max_requests: int = Field(default=20, ge=1, le=100)
    max_depth: int = Field(default=8, ge=1, le=20)
    sort: Literal[
        "provider", "confidence", "top", "new", "controversial", "old", "qa", "live"
    ] | None = None


def build_comments_router(
    get_store: Callable[[], PersistenceStore],
    get_connectors: Callable[[], Mapping[str, SourceConnector]],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/items")
    scan_lock = asyncio.Lock()

    @router.get("/{content_item_id}/comments")
    async def list_comments(
        content_item_id: int,
        limit: int = Query(default=200, ge=1, le=1_000),
    ):
        store = get_store()
        item = await asyncio.to_thread(store.item, content_item_id)
        if item is None:
            raise HTTPException(404, "Content item not found")
        rows = await asyncio.to_thread(
            store.comments_for_content,
            str(item["source_id"]),
            str(item["external_id"]),
            limit=limit,
        )
        return {
            "content_item_id": content_item_id,
            "source_id": item["source_id"],
            "content_external_id": item["external_id"],
            "count": len(rows),
            "items": rows,
        }

    @router.post("/{content_item_id}/comments/scan")
    async def scan_comments(
        content_item_id: int,
        payload: CommentScanRequest,
    ):
        store = get_store()
        item = await asyncio.to_thread(store.item, content_item_id)
        if item is None:
            raise HTTPException(404, "Content item not found")
        source_id = str(item.get("source_id") or "")
        if (
            source_id
            not in {
                "bilibili",
                "bluesky",
                "mastodon",
                "reddit",
                "youtube",
                "tieba",
                "x",
            }
            or not SOURCE_REGISTRY.executable(source_id, Operation.LIST_COMMENTS)
        ):
            raise HTTPException(
                409,
                "This source does not expose a runnable manual comment provider.",
            )
        provider_id, _ = SOURCE_REGISTRY.executable_provider(
            source_id, Operation.LIST_COMMENTS
        )
        connector = get_connectors().get(source_id)
        scan = getattr(connector, "scan_comments", None)
        if not callable(scan):
            raise HTTPException(503, "Comment provider is unavailable.")
        if source_id == "reddit":
            budgets: (
                BlueskyCommentBudgets
                | MastodonCommentBudgets
                | RedditCommentBudgets
                | YouTubeCommentBudgets
                | RunBudgets
            ) = (
                RedditCommentBudgets(
                    max_root_comments=payload.max_root_comments,
                    max_children_per_root=payload.max_children_per_root,
                    max_total_comments=payload.max_total_comments,
                    max_requests=payload.max_requests,
                    max_depth=payload.max_depth,
                )
            )
        elif source_id == "youtube":
            budgets = YouTubeCommentBudgets(
                max_root_comments=payload.max_root_comments,
                max_children_per_root=payload.max_children_per_root,
                max_total_comments=payload.max_total_comments,
                max_requests=payload.max_requests,
            )
        elif source_id == "bluesky":
            budgets = BlueskyCommentBudgets(
                max_root_comments=payload.max_root_comments,
                max_children_per_root=payload.max_children_per_root,
                max_total_comments=payload.max_total_comments,
                max_requests=payload.max_requests,
                max_depth=payload.max_depth,
            )
        elif source_id == "mastodon":
            budgets = MastodonCommentBudgets(
                max_root_comments=payload.max_root_comments,
                max_children_per_root=payload.max_children_per_root,
                max_total_comments=payload.max_total_comments,
                max_requests=payload.max_requests,
                max_depth=payload.max_depth,
            )
        elif source_id in {"bilibili", "tieba"}:
            cbce_provider_id = f"cbce_{source_id}"
            rollout = cbce_provider_rollout_status(
                source_id, cbce_provider_id, Operation.LIST_COMMENTS
            )
            if not rollout or not rollout["ready"]:
                raise HTTPException(
                    503,
                    str(
                        (rollout or {}).get("detail")
                        or f"{source_id.title()} clean-room comment provider is unavailable."
                    ),
                )
            root_limit = min(
                payload.max_root_comments, payload.max_total_comments
            )
            budgets = RunBudgets(
                max_items=root_limit,
                max_requests=payload.max_requests,
                deadline_seconds=900,
                max_root_comments=root_limit,
                max_children_per_root=payload.max_children_per_root,
                max_total_comments=payload.max_total_comments,
            )
        else:
            budgets = RunBudgets(
                max_items=payload.max_total_comments,
                max_requests=payload.max_requests,
                deadline_seconds=900,
                max_root_comments=payload.max_root_comments,
                max_children_per_root=payload.max_children_per_root,
                max_total_comments=payload.max_total_comments,
            )
        try:
            selected_sort = payload.sort or (
                "provider" if source_id in {"bluesky", "mastodon"} else "new"
            )
            async with scan_lock:
                result = await scan(
                    str(item.get("canonical_url") or item["external_id"]),
                    budgets,
                    sort=selected_sort,
                )
                resolved_content_id = getattr(result, "content_external_id", None)
                if (
                    resolved_content_id is not None
                    and str(resolved_content_id) != str(item["external_id"])
                ):
                    raise CrawlerFailure(
                        CrawlerErrorCode.PARSE_CHANGED,
                        "Comment provider resolved a different content identity.",
                    )
                records = tuple(result.records)
                if result.provider_id != provider_id:
                    raise CrawlerFailure(
                        CrawlerErrorCode.PARSE_CHANGED,
                        "Comment provider identity does not match the registry.",
                    )
                root_count = result.root_count
                child_count = result.child_count
                request_count = result.request_count
                truncated = result.truncated
                if source_id == "bilibili":
                    (
                        records,
                        child_count,
                        request_count,
                        truncated,
                    ) = await _expand_bilibili_children(
                        connector,
                        str(item.get("canonical_url") or item["external_id"]),
                        records,
                        provider_id=provider_id,
                        payload=payload,
                        request_count=request_count,
                        truncated=bool(
                            getattr(result, "pagination_truncated", truncated)
                        ),
                    )
                _validate_records(item, records)
                observed_at = datetime.now(UTC)

                def persist() -> int:
                    for record in records:
                        store.save_comment(
                            _comment_values(
                                record,
                                content_item_id=content_item_id,
                                observed_at=observed_at,
                            )
                        )
                    return len(records)

                stored_count = await asyncio.to_thread(persist)
        except CrawlerFailure as exc:
            raise _http_failure(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "content_item_id": content_item_id,
            "source_id": source_id,
            "content_external_id": item["external_id"],
            "fetched_count": len(records),
            "stored_count": stored_count,
            "root_count": root_count,
            "child_count": child_count,
            "request_count": request_count,
            "truncated": truncated,
            "provider_id": result.provider_id,
        }

    return router


async def _expand_bilibili_children(
    connector: SourceConnector,
    target_url: str,
    roots: tuple[CommentRecord, ...],
    *,
    provider_id: str,
    payload: CommentScanRequest,
    request_count: int,
    truncated: bool,
) -> tuple[tuple[CommentRecord, ...], int, int, bool]:
    """Fetch stable child identities without weakening the shared hierarchy gate."""
    scan_children = getattr(connector, "scan_child_comments", None)
    if not callable(scan_children):
        return roots, 0, request_count, truncated
    ordered: list[CommentRecord] = []
    child_count = 0
    remaining_child_total = max(payload.max_total_comments - len(roots), 0)
    for root in roots:
        ordered.append(root)
        declared = root.child_count
        if declared <= 0:
            continue
        remaining_requests = payload.max_requests - request_count
        allowance = min(
            declared,
            payload.max_children_per_root,
            remaining_child_total,
        )
        if allowance <= 0 or remaining_requests <= 0:
            truncated = True
            continue
        child_scan = await scan_children(
            target_url,
            root.external_id,
            RunBudgets(
                max_items=allowance,
                max_requests=remaining_requests,
                deadline_seconds=900,
                max_children_per_root=allowance,
                max_total_comments=allowance,
            ),
            sort="new",
        )
        if child_scan.provider_id != provider_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Child-comment provider identity does not match the registry.",
            )
        children = tuple(child_scan.records[:allowance])
        ordered.extend(children)
        child_count += len(children)
        remaining_child_total -= len(children)
        request_count += child_scan.request_count
        child_pagination_truncated = bool(
            getattr(child_scan, "pagination_truncated", child_scan.truncated)
        )
        if (
            child_pagination_truncated
            or declared > allowance
            or len(children) < min(declared, allowance)
        ):
            truncated = True
    return tuple(ordered), child_count, request_count, truncated


def _validate_records(
    item: Mapping[str, Any], records: tuple[CommentRecord, ...]
) -> None:
    source_id = str(item.get("source_id") or "")
    external_id = str(item.get("external_id") or "")
    seen: set[str] = set()
    for record in records:
        if record.source_id != source_id or record.content_external_id != external_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Comment provider returned a mismatched content identity.",
            )
        if record.external_id in seen:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Comment provider returned a duplicate identity.",
            )
        if record.parent_external_id == record.external_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Comment provider returned a self-referential hierarchy.",
            )
        if record.parent_external_id is None:
            if record.root_external_id not in {None, record.external_id}:
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Comment provider returned an invalid root hierarchy.",
                )
        elif (
            record.parent_external_id
            not in (seen | ({external_id} if source_id == "x" else set()))
            or record.root_external_id
            not in (seen | ({external_id} if source_id == "x" else set()))
        ):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Comment provider returned an orphaned hierarchy.",
            )
        seen.add(record.external_id)


def _comment_values(
    record: CommentRecord,
    *,
    content_item_id: int,
    observed_at: datetime,
) -> dict[str, Any]:
    return {
        "source_id": record.source_id,
        "external_id": record.external_id,
        "content_external_id": record.content_external_id,
        "content_item_id": content_item_id,
        "body": record.body,
        "author_pseudonym": record.author_pseudonym,
        "published_at": record.published_at,
        "like_count": record.like_count,
        "child_count": record.child_count,
        "parent_external_id": record.parent_external_id,
        "root_external_id": record.root_external_id,
        "provenance": dict(record.provenance),
        "last_seen_at": observed_at,
    }


def _http_failure(error: CrawlerFailure) -> HTTPException:
    status = {
        CrawlerErrorCode.AUTH_REQUIRED: 401,
        CrawlerErrorCode.PERMISSION_REQUIRED: 403,
        CrawlerErrorCode.NOT_FOUND: 404,
        CrawlerErrorCode.RATE_LIMITED: 429,
        CrawlerErrorCode.BUDGET_EXHAUSTED: 422,
        CrawlerErrorCode.UNSUPPORTED: 409,
        CrawlerErrorCode.PARSE_CHANGED: 502,
        CrawlerErrorCode.TRANSPORT_ERROR: 503,
    }.get(error.code, 500)
    headers = None
    if error.retry_after_seconds is not None:
        headers = {"Retry-After": str(max(0, int(error.retry_after_seconds)))}
    return HTTPException(status, error.safe_message, headers=headers)
