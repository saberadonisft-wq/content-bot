"""Bounded Reddit comment trees through the documented OAuth read API.

The adapter deliberately models Reddit's `more` stubs instead of treating the
first rendered tree as complete.  It never fetches more than 100 child IDs in
one request and serializes all `/api/morechildren` calls, as required by the
provider documentation.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from ...runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)

_ID36 = re.compile(r"^[a-z0-9]{3,16}$", re.IGNORECASE)
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_SORTS = frozenset(
    {"confidence", "top", "new", "controversial", "old", "qa", "live"}
)


@dataclass(frozen=True, slots=True)
class RedditPostTarget:
    post_id: str
    canonical_url: str


def parse_reddit_post_target(value: str) -> RedditPostTarget:
    raw = str(value or "").strip()
    if _ID36.fullmatch(raw):
        post_id = raw.casefold()
        return RedditPostTarget(
            post_id, f"https://www.reddit.com/comments/{post_id}"
        )
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("Reddit post target is invalid") from exc
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        raise ValueError("Reddit post target must be a public HTTPS URL")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host == "redd.it":
        parts = [part for part in parsed.path.split("/") if part]
        post_id = parts[0].casefold() if len(parts) == 1 else ""
    elif host == "reddit.com" or host.endswith(".reddit.com"):
        parts = [part for part in parsed.path.split("/") if part]
        folded = [part.casefold() for part in parts]
        post_id = ""
        if "comments" in folded:
            index = folded.index("comments")
            if index + 1 < len(parts):
                post_id = parts[index + 1].casefold()
    else:
        raise ValueError("Reddit post host is not supported")
    if not _ID36.fullmatch(post_id):
        raise ValueError("Reddit post ID is invalid")
    return RedditPostTarget(post_id, f"https://www.reddit.com/comments/{post_id}")


@dataclass(frozen=True, slots=True)
class RedditCommentBudgets:
    max_root_comments: int = 100
    max_children_per_root: int = 100
    max_total_comments: int = 300
    max_requests: int = 20
    max_depth: int = 8

    def __post_init__(self) -> None:
        if not 1 <= self.max_root_comments <= 500:
            raise ValueError("Root comment budget must be between 1 and 500")
        if not 0 <= self.max_children_per_root <= 500:
            raise ValueError("Child comment budget must be between 0 and 500")
        if not 1 <= self.max_total_comments <= 1_000:
            raise ValueError("Total comment budget must be between 1 and 1000")
        if not 1 <= self.max_requests <= 50:
            raise ValueError("Comment request budget must be between 1 and 50")
        if not 1 <= self.max_depth <= 20:
            raise ValueError("Comment depth budget must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class RedditCommentScan:
    target: RedditPostTarget
    records: tuple[CommentRecord, ...]
    request_count: int
    root_count: int
    child_count: int
    truncated: bool
    provider_id: str = "reddit_public"


class RedditCommentProvider(Protocol):
    request_count: int

    async def comment_tree(
        self,
        post_id: str,
        *,
        limit: int,
        depth: int,
        sort: str,
        cancellation: CancellationToken,
    ) -> Sequence[Mapping[str, Any]]: ...

    async def more_children(
        self,
        post_id: str,
        child_ids: Sequence[str],
        *,
        depth: int,
        sort: str,
        cancellation: CancellationToken,
    ) -> Sequence[Mapping[str, Any]]: ...


class RedditApiCommentProvider:
    """Small typed transport for documented Reddit read endpoints."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_requests: int,
        attempts: int = 3,
    ) -> None:
        if not 1 <= max_requests <= 50 or not 1 <= attempts <= 5:
            raise ValueError("Reddit comment transport limits are invalid")
        self.client = client
        self.max_requests = max_requests
        self.attempts = attempts
        self.request_count = 0

    async def comment_tree(
        self,
        post_id: str,
        *,
        limit: int,
        depth: int,
        sort: str,
        cancellation: CancellationToken,
    ) -> Sequence[Mapping[str, Any]]:
        payload = await self._get_json(
            f"https://oauth.reddit.com/comments/{post_id}",
            params={
                "limit": min(max(limit, 1), 500),
                "depth": depth,
                "sort": sort,
                "raw_json": 1,
            },
            cancellation=cancellation,
        )
        if not isinstance(payload, list) or len(payload) < 2:
            raise _parse_changed("Reddit returned an invalid comment tree.")
        return _listing_children(payload[1])

    async def more_children(
        self,
        post_id: str,
        child_ids: Sequence[str],
        *,
        depth: int,
        sort: str,
        cancellation: CancellationToken,
    ) -> Sequence[Mapping[str, Any]]:
        ids = tuple(dict.fromkeys(str(value).casefold() for value in child_ids))
        if not ids or len(ids) > 100 or any(not _ID36.fullmatch(value) for value in ids):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Reddit more-comments identities are invalid.",
            )
        payload = await self._get_json(
            "https://oauth.reddit.com/api/morechildren",
            params={
                "api_type": "json",
                "children": ",".join(ids),
                "depth": depth,
                "limit_children": "true",
                "link_id": f"t3_{post_id}",
                "sort": sort,
                "raw_json": 1,
            },
            cancellation=cancellation,
        )
        try:
            things = payload["json"]["data"]["things"]
        except (KeyError, TypeError) as exc:
            raise _parse_changed("Reddit returned invalid additional comments.") from exc
        if not isinstance(things, list):
            raise _parse_changed("Reddit returned invalid additional comments.")
        return tuple(value for value in things if isinstance(value, Mapping))

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any],
        cancellation: CancellationToken,
    ) -> Any:
        response: httpx.Response | None = None
        for attempt in range(self.attempts):
            cancellation.raise_if_cancelled()
            if self.request_count >= self.max_requests:
                raise CrawlerFailure(
                    CrawlerErrorCode.BUDGET_EXHAUSTED,
                    "Reddit comment request budget was exhausted.",
                )
            self.request_count += 1
            try:
                response = await self.client.get(url, params=dict(params))
            except httpx.TransportError as exc:
                if attempt + 1 >= self.attempts:
                    raise CrawlerFailure(
                        CrawlerErrorCode.TRANSPORT_ERROR,
                        "Reddit comments are temporarily unavailable.",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            status = response.status_code
            if status not in _RETRYABLE or attempt + 1 >= self.attempts:
                break
            await asyncio.sleep(_retry_delay(response, attempt))
        assert response is not None
        if response.status_code >= 400:
            raise _response_failure(response)
        try:
            return response.json()
        except ValueError as exc:
            raise _parse_changed("Reddit returned invalid comment JSON.") from exc


@dataclass(frozen=True, slots=True)
class _MoreStub:
    parent_fullname: str
    child_ids: tuple[str, ...]
    root_external_id: str | None
    depth: int


class RedditCommentsAdapter:
    source_id = "reddit"
    provider_id = "reddit_public"

    def __init__(
        self,
        provider: RedditCommentProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer

    async def crawl(
        self,
        target_value: str,
        budgets: RedditCommentBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
    ) -> RedditCommentScan:
        if sort not in _SORTS:
            raise ValueError("Unsupported Reddit comment sort")
        token = cancellation or CancellationToken()
        target = parse_reddit_post_target(target_value)
        initial = await self.provider.comment_tree(
            target.post_id,
            limit=min(budgets.max_total_comments, 500),
            depth=budgets.max_depth,
            sort=sort,
            cancellation=token,
        )
        records: list[CommentRecord] = []
        seen: set[str] = set()
        roots: set[str] = set()
        root_by_id: dict[str, str] = {}
        children_by_root: dict[str, int] = {}
        more: deque[_MoreStub] = deque()
        requested_more_ids: set[str] = set()
        truncated = False
        link_fullname = f"t3_{target.post_id}"

        def add_thing(
            thing: Mapping[str, Any],
            inherited_root: str | None,
            depth: int,
        ) -> None:
            nonlocal truncated
            if len(records) >= budgets.max_total_comments:
                truncated = True
                return
            kind = str(thing.get("kind") or "")
            data = thing.get("data")
            if not isinstance(data, Mapping):
                return
            if kind == "more":
                ids = tuple(
                    value.casefold()
                    for value in map(str, data.get("children") or ())
                    if _ID36.fullmatch(value)
                )
                parent = str(data.get("parent_id") or "")
                if ids and depth <= budgets.max_depth:
                    more.append(_MoreStub(parent, ids, inherited_root, depth))
                return
            if kind != "t1":
                return
            external_id = _comment_id(data)
            if not external_id or external_id in seen:
                return
            parent_fullname = str(data.get("parent_id") or "")
            parent_id = _strip_comment_fullname(parent_fullname)
            is_root = parent_fullname == link_fullname
            if not is_root and (parent_id is None or parent_id not in root_by_id):
                return
            root_id = (
                external_id
                if is_root
                else inherited_root or root_by_id.get(parent_id or "")
            )
            if root_id is None:
                return
            if is_root:
                if len(roots) >= budgets.max_root_comments:
                    truncated = True
                    return
                roots.add(root_id)
                children_by_root.setdefault(root_id, 0)
            else:
                child_count = children_by_root.get(root_id, 0)
                if child_count >= budgets.max_children_per_root:
                    truncated = True
                    return
                children_by_root[root_id] = child_count + 1
            seen.add(external_id)
            root_by_id[external_id] = root_id
            records.append(
                _normalize_comment(
                    data,
                    post_id=target.post_id,
                    external_id=external_id,
                    parent_external_id=None if is_root else parent_id,
                    root_external_id=root_id,
                    pseudonymizer=self.pseudonymizer,
                )
            )
            if depth >= budgets.max_depth:
                if _reply_children(data):
                    truncated = True
                return
            for reply in _reply_children(data):
                add_thing(reply, root_id, depth + 1)

        for thing in initial:
            token.raise_if_cancelled()
            add_thing(thing, None, 1)
            if len(records) >= budgets.max_total_comments:
                break

        while more and len(records) < budgets.max_total_comments:
            token.raise_if_cancelled()
            stub = more.popleft()
            root_id = stub.root_external_id
            if root_id is None and stub.parent_fullname.startswith("t1_"):
                root_id = root_by_id.get(stub.parent_fullname[3:])
            if root_id is None:
                remaining = min(
                    budgets.max_root_comments - len(roots),
                    budgets.max_total_comments - len(records),
                )
            else:
                remaining = min(
                    budgets.max_children_per_root - children_by_root.get(root_id, 0),
                    budgets.max_total_comments - len(records),
                )
            candidates = tuple(
                value
                for value in stub.child_ids
                if value not in requested_more_ids
            )
            if remaining <= 0 or not candidates:
                truncated = truncated or bool(candidates)
                continue
            batch = candidates[: min(100, remaining)]
            requested_more_ids.update(batch)
            if len(candidates) > len(batch):
                more.appendleft(
                    _MoreStub(
                        stub.parent_fullname,
                        candidates[len(batch) :],
                        root_id,
                        stub.depth,
                    )
                )
            try:
                things = await self.provider.more_children(
                    target.post_id,
                    batch,
                    depth=max(1, budgets.max_depth - stub.depth + 1),
                    sort=sort,
                    cancellation=token,
                )
            except CrawlerFailure as exc:
                if exc.code is CrawlerErrorCode.BUDGET_EXHAUSTED:
                    truncated = True
                    break
                raise
            before = len(records)
            deferred: list[Mapping[str, Any]] = []
            for thing in things:
                data = thing.get("data") if isinstance(thing, Mapping) else None
                if (
                    isinstance(data, Mapping)
                    and str(thing.get("kind") or "") == "t1"
                    and str(data.get("parent_id") or "").startswith("t1_")
                    and _strip_comment_fullname(str(data.get("parent_id") or ""))
                    not in root_by_id
                    and root_id is None
                ):
                    deferred.append(thing)
                    continue
                add_thing(thing, root_id, stub.depth)
            for thing in deferred:
                add_thing(thing, root_id, stub.depth)
            if len(records) == before and not any(
                str(thing.get("kind") or "") == "more" for thing in things
            ):
                truncated = True

        if more or len(records) >= budgets.max_total_comments:
            truncated = True
        child_total = len(records) - len(roots)
        return RedditCommentScan(
            target=target,
            records=tuple(records),
            request_count=self.provider.request_count,
            root_count=len(roots),
            child_count=child_total,
            truncated=truncated,
        )


def _normalize_comment(
    data: Mapping[str, Any],
    *,
    post_id: str,
    external_id: str,
    parent_external_id: str | None,
    root_external_id: str,
    pseudonymizer: IdentityPseudonymizer,
) -> CommentRecord:
    author = str(data.get("author") or "").strip()
    score = _integer(data.get("score"))
    return CommentRecord(
        source_id="reddit",
        external_id=external_id,
        content_external_id=post_id,
        body=str(data.get("body") or "")[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("reddit", author),
        published_at=_timestamp(data.get("created_utc")),
        like_count=max(0, score),
        child_count=max(0, _integer(data.get("num_comments"))),
        parent_external_id=parent_external_id,
        root_external_id=root_external_id,
        provenance={
            "provider_id": "reddit_public",
            "contract_version": "cbce.reddit.comments.v1",
            "coverage": "bounded_official_comment_tree",
            "reddit_score": score,
        },
    )


def _listing_children(value: Any) -> tuple[Mapping[str, Any], ...]:
    try:
        children = value["data"]["children"]
    except (KeyError, TypeError) as exc:
        raise _parse_changed("Reddit returned an invalid comment listing.") from exc
    if not isinstance(children, list):
        raise _parse_changed("Reddit returned an invalid comment listing.")
    return tuple(child for child in children if isinstance(child, Mapping))


def _reply_children(data: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    replies = data.get("replies")
    if not isinstance(replies, Mapping):
        return ()
    return _listing_children(replies)


def _comment_id(data: Mapping[str, Any]) -> str:
    fullname = str(data.get("name") or "")
    value = fullname[3:] if fullname.startswith("t1_") else str(data.get("id") or "")
    value = value.casefold()
    return value if _ID36.fullmatch(value) else ""


def _strip_comment_fullname(value: str) -> str | None:
    candidate = value[3:].casefold() if value.startswith("t1_") else ""
    return candidate if _ID36.fullmatch(candidate) else None


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _parse_changed(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    try:
        return min(max(float(value), 0.0), 5.0) if value else 0.25 * (2**attempt)
    except (TypeError, ValueError):
        return 0.25 * (2**attempt)


def _response_failure(response: httpx.Response) -> CrawlerFailure:
    status = response.status_code
    if status == 401:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "Reddit OAuth credentials were rejected or expired.",
        )
    if status == 403:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "Reddit comments are not accessible for this post.",
        )
    if status == 404:
        return CrawlerFailure(
            CrawlerErrorCode.NOT_FOUND,
            "Reddit post was not found.",
        )
    if status == 429:
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "Reddit comment rate limit was reached.",
            retryable=True,
            retry_after_seconds=_retry_delay(response, 0),
        )
    return CrawlerFailure(
        CrawlerErrorCode.TRANSPORT_ERROR,
        "Reddit comments are temporarily unavailable.",
        retryable=status >= 500,
    )
