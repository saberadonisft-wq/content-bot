"""Bounded public Bluesky reply trees through the documented AppView query.

The public ``getPostThread`` response is a bounded tree rather than a cursor
listing.  This adapter therefore applies root, descendant, depth, total and
physical-request budgets locally and marks any omitted branch as truncated.
Raw DIDs and AT URIs are used only in memory to validate the tree; persistence
receives stable hashes and HMAC author pseudonyms.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from ....services.bluesky_api import bluesky_json, post_identity, stable_post_id
from ...runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)

_DID = re.compile(r"^did:[a-z0-9]+:[A-Za-z0-9._:%-]{1,512}$")
_RKEY = re.compile(r"^[A-Za-z0-9._:~-]{1,512}$")
_HANDLE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class BlueskyPostTarget:
    actor: str
    record_key: str
    canonical_url: str


def parse_bluesky_post_target(value: str) -> BlueskyPostTarget:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("Bluesky post target is invalid") from exc
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        raise ValueError("Bluesky post target must be a public HTTPS URL")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in {"bsky.app", "www.bsky.app"}:
        raise ValueError("Bluesky post host is not supported")
    if "%" in parsed.path:
        raise ValueError("Bluesky post path must not be encoded")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[0].casefold() != "profile" or parts[2].casefold() != "post":
        raise ValueError("Bluesky post path is invalid")
    raw_actor = parts[1]
    actor = raw_actor if _DID.fullmatch(raw_actor) else raw_actor.casefold()
    record_key = parts[3]
    if not (_DID.fullmatch(actor) or _valid_handle(actor)):
        raise ValueError("Bluesky actor is invalid")
    if not _RKEY.fullmatch(record_key):
        raise ValueError("Bluesky post record key is invalid")
    return BlueskyPostTarget(
        actor=actor,
        record_key=record_key,
        canonical_url=f"https://bsky.app/profile/{actor}/post/{record_key}",
    )


@dataclass(frozen=True, slots=True)
class BlueskyCommentBudgets:
    max_root_comments: int = 100
    max_children_per_root: int = 100
    max_total_comments: int = 300
    max_requests: int = 5
    max_depth: int = 8

    def __post_init__(self) -> None:
        if not 1 <= self.max_root_comments <= 500:
            raise ValueError("Root comment budget must be between 1 and 500")
        if not 0 <= self.max_children_per_root <= 500:
            raise ValueError("Child comment budget must be between 0 and 500")
        if not 1 <= self.max_total_comments <= 1_000:
            raise ValueError("Total comment budget must be between 1 and 1000")
        if not 1 <= self.max_requests <= 100:
            raise ValueError("Comment request budget must be between 1 and 100")
        if not 1 <= self.max_depth <= 20:
            raise ValueError("Comment depth budget must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class BlueskyCommentScan:
    target: BlueskyPostTarget
    content_external_id: str
    records: tuple[CommentRecord, ...]
    request_count: int
    root_count: int
    child_count: int
    truncated: bool
    provider_id: str = "bluesky_public"


class BlueskyCommentProvider(Protocol):
    request_count: int

    async def resolve_handle(
        self,
        handle: str,
        *,
        cancellation: CancellationToken,
    ) -> str: ...

    async def post_thread(
        self,
        uri: str,
        *,
        depth: int,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]: ...


class BlueskyApiCommentProvider:
    """Typed transport for the public resolve-handle and post-thread XRPCs."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_requests: int,
        attempts: int = 3,
    ) -> None:
        if not 1 <= max_requests <= 100 or not 1 <= attempts <= 5:
            raise ValueError("Bluesky comment transport limits are invalid")
        self.client = client
        self.max_requests = max_requests
        self.attempts = attempts
        self.request_count = 0

    async def resolve_handle(
        self,
        handle: str,
        *,
        cancellation: CancellationToken,
    ) -> str:
        payload = await bluesky_json(
            self.client,
            "/xrpc/com.atproto.identity.resolveHandle",
            params={"handle": handle},
            attempts=self.attempts,
            before_request=lambda: self._spend(cancellation),
        )
        did = str(payload.get("did") or "")
        if not _DID.fullmatch(did):
            raise _parse_changed("Bluesky handle resolution returned an invalid DID.")
        return did

    async def post_thread(
        self,
        uri: str,
        *,
        depth: int,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]:
        return await bluesky_json(
            self.client,
            "/xrpc/app.bsky.feed.getPostThread",
            params={"uri": uri, "depth": depth, "parentHeight": 0},
            attempts=self.attempts,
            before_request=lambda: self._spend(cancellation),
        )

    def _spend(self, cancellation: CancellationToken) -> None:
        cancellation.raise_if_cancelled()
        if self.request_count >= self.max_requests:
            raise CrawlerFailure(
                CrawlerErrorCode.BUDGET_EXHAUSTED,
                "Bluesky comment request budget was exhausted.",
            )
        self.request_count += 1


class BlueskyCommentsAdapter:
    source_id = "bluesky"
    provider_id = "bluesky_public"

    def __init__(
        self,
        provider: BlueskyCommentProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer

    async def crawl(
        self,
        target_value: str,
        budgets: BlueskyCommentBudgets,
        *,
        sort: str = "provider",
        cancellation: CancellationToken | None = None,
    ) -> BlueskyCommentScan:
        if sort != "provider":
            raise ValueError(
                "Bluesky AppView does not expose a selectable thread sort; use 'provider'."
            )
        token = cancellation or CancellationToken()
        target = parse_bluesky_post_target(target_value)
        did = (
            target.actor
            if _DID.fullmatch(target.actor)
            else await self.provider.resolve_handle(target.actor, cancellation=token)
        )
        content_uri = f"at://{did}/app.bsky.feed.post/{target.record_key}"
        content_external_id = stable_post_id(content_uri)
        if content_external_id is None:
            raise _parse_changed("Bluesky post identity is invalid.")
        payload = await self.provider.post_thread(
            content_uri,
            depth=budgets.max_depth,
            cancellation=token,
        )
        root_node = payload.get("thread")
        if not isinstance(root_node, Mapping):
            raise _parse_changed("Bluesky returned an invalid post thread.")
        root_post = _thread_post(root_node, root=True)
        root_identity = post_identity(dict(root_post))
        if root_identity is None or root_identity[0] != content_uri:
            raise _parse_changed("Bluesky returned a thread for a different post.")

        records: list[CommentRecord] = []
        seen: set[str] = set()
        child_counts: dict[str, int] = {}
        truncated = False

        def add_node(
            node: Mapping[str, Any],
            *,
            parent_uri: str,
            parent_external_id: str | None,
            root_external_id: str | None,
            depth: int,
        ) -> None:
            nonlocal truncated
            token.raise_if_cancelled()
            if _unavailable_node(node):
                truncated = True
                return
            post = _thread_post(node)
            identity = post_identity(dict(post))
            if identity is None:
                raise _parse_changed("Bluesky reply identity is invalid.")
            uri, _record_key = identity
            external_id = stable_post_id(uri)
            if external_id is None:
                raise _parse_changed("Bluesky reply identity is invalid.")
            if external_id in seen:
                truncated = True
                return
            record = _record(post)
            reply = record.get("reply")
            if not isinstance(reply, Mapping):
                raise _parse_changed("Bluesky reply relation is missing.")
            actual_parent = str(_mapping(reply.get("parent")).get("uri") or "")
            actual_root = str(_mapping(reply.get("root")).get("uri") or "")
            if actual_parent != parent_uri or actual_root != content_uri:
                raise _parse_changed("Bluesky reply hierarchy does not match the thread.")

            if len(records) >= budgets.max_total_comments:
                truncated = True
                return
            is_root = parent_external_id is None
            if is_root:
                if sum(1 for value in records if value.parent_external_id is None) >= budgets.max_root_comments:
                    truncated = True
                    return
                comment_root = external_id
                child_counts.setdefault(comment_root, 0)
            else:
                comment_root = root_external_id
                if comment_root is None:
                    raise _parse_changed("Bluesky reply root identity is missing.")
                if child_counts.get(comment_root, 0) >= budgets.max_children_per_root:
                    truncated = True
                    return
                child_counts[comment_root] = child_counts.get(comment_root, 0) + 1
            replies = _replies(node)
            declared_children = _nonnegative_int(post.get("replyCount"), "reply count")
            record_value = _normalize_comment(
                post,
                content_external_id=content_external_id,
                external_id=external_id,
                parent_external_id=parent_external_id,
                root_external_id=comment_root,
                declared_children=declared_children,
                pseudonymizer=self.pseudonymizer,
            )
            seen.add(external_id)
            records.append(record_value)
            if declared_children > len(replies):
                truncated = True
            if depth >= budgets.max_depth:
                if replies or declared_children:
                    truncated = True
                return
            for child in replies:
                if len(records) >= budgets.max_total_comments:
                    truncated = True
                    break
                add_node(
                    child,
                    parent_uri=uri,
                    parent_external_id=external_id,
                    root_external_id=comment_root,
                    depth=depth + 1,
                )

        root_replies = _replies(root_node)
        declared_roots = _nonnegative_int(root_post.get("replyCount"), "root reply count")
        if declared_roots > len(root_replies):
            truncated = True
        for node in root_replies:
            if len(records) >= budgets.max_total_comments:
                truncated = True
                break
            add_node(
                node,
                parent_uri=content_uri,
                parent_external_id=None,
                root_external_id=None,
                depth=1,
            )
        root_count = sum(1 for record in records if record.parent_external_id is None)
        if len(root_replies) > root_count or declared_roots > root_count:
            truncated = True
        return BlueskyCommentScan(
            target=target,
            content_external_id=content_external_id,
            records=tuple(records),
            request_count=self.provider.request_count,
            root_count=root_count,
            child_count=len(records) - root_count,
            truncated=truncated,
        )


def _normalize_comment(
    post: Mapping[str, Any],
    *,
    content_external_id: str,
    external_id: str,
    parent_external_id: str | None,
    root_external_id: str,
    declared_children: int,
    pseudonymizer: IdentityPseudonymizer,
) -> CommentRecord:
    record = _record(post)
    author = post.get("author")
    if not isinstance(author, Mapping):
        raise _parse_changed("Bluesky reply author is missing.")
    did = str(author.get("did") or "")
    if not _DID.fullmatch(did):
        raise _parse_changed("Bluesky reply author identity is invalid.")
    return CommentRecord(
        source_id="bluesky",
        external_id=external_id,
        content_external_id=content_external_id,
        body=str(record.get("text") or "")[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("bluesky", did),
        published_at=_timestamp(record.get("createdAt") or post.get("indexedAt")),
        like_count=_nonnegative_int(post.get("likeCount"), "like count"),
        child_count=declared_children,
        parent_external_id=parent_external_id,
        root_external_id=root_external_id,
        provenance={
            "provider_id": "bluesky_public",
            "contract_version": "cbce.bluesky.comments.v1",
            "coverage": "bounded_public_post_thread",
            "order": "provider_defined",
        },
    )


def _thread_post(
    node: Mapping[str, Any],
    *,
    root: bool = False,
) -> Mapping[str, Any]:
    if _unavailable_node(node):
        if root and bool(node.get("notFound")):
            raise CrawlerFailure(
                CrawlerErrorCode.NOT_FOUND,
                "Bluesky post was not found.",
            )
        if root and bool(node.get("blocked")):
            raise CrawlerFailure(
                CrawlerErrorCode.PERMISSION_REQUIRED,
                "Bluesky did not expose this public post thread.",
            )
        raise _parse_changed("Bluesky thread node is unavailable.")
    post = node.get("post")
    if not isinstance(post, Mapping):
        raise _parse_changed("Bluesky returned an invalid thread node.")
    return post


def _record(post: Mapping[str, Any]) -> Mapping[str, Any]:
    value = post.get("record")
    if not isinstance(value, Mapping):
        raise _parse_changed("Bluesky reply record is missing.")
    return value


def _replies(node: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    value = node.get("replies")
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _parse_changed("Bluesky thread replies are invalid.")
    if any(not isinstance(item, Mapping) for item in value):
        raise _parse_changed("Bluesky thread replies are invalid.")
    return tuple(value)  # type: ignore[arg-type]


def _unavailable_node(node: Mapping[str, Any]) -> bool:
    return bool(node.get("notFound")) or bool(node.get("blocked"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _parse_changed("Bluesky reply timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _nonnegative_int(value: Any, label: str) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise _parse_changed(f"Bluesky {label} is invalid.") from exc
    if parsed < 0:
        raise _parse_changed(f"Bluesky {label} is invalid.")
    return parsed


def _valid_handle(value: str) -> bool:
    if not 1 <= len(value) <= 253 or "." not in value:
        return False
    labels = value.split(".")
    return all(_HANDLE_LABEL.fullmatch(label) for label in labels)


def _parse_changed(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)
