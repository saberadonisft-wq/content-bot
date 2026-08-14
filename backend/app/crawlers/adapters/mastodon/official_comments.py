"""Bounded public Mastodon status descendants from documented REST endpoints."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from ....services.mastodon_api import (
    mastodon_json,
    normalize_status,
)
from ...runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)
from ...target_detection import InvalidTargetUrl, normalize_host, normalize_target_url

_STATUS_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass(frozen=True, slots=True)
class MastodonStatusTarget:
    instance: str
    status_id: str
    canonical_url: str


def parse_mastodon_status_target(
    value: str,
    *,
    allowed_instances: Iterable[str],
) -> MastodonStatusTarget:
    raw = str(value or "").strip()
    try:
        normalized, host = normalize_target_url(raw)
    except InvalidTargetUrl as exc:
        raise ValueError("Mastodon status target is invalid") from exc
    parsed = urlsplit(normalized)
    allowed = {normalize_host(instance) for instance in allowed_instances}
    if parsed.scheme != "https" or host not in allowed or parsed.port not in {None, 443}:
        raise ValueError("Mastodon status instance is not configured")
    if "%" in parsed.path:
        raise ValueError("Mastodon status path must not be encoded")
    parts = [part for part in parsed.path.split("/") if part]
    status_id = ""
    canonical_path = ""
    if (
        len(parts) == 2
        and parts[0].startswith("@")
        and _ACCOUNT.fullmatch(parts[0][1:])
    ):
        status_id = parts[1]
        canonical_path = f"/{parts[0]}/{status_id}"
    elif (
        len(parts) == 4
        and parts[0].casefold() == "users"
        and _ACCOUNT.fullmatch(parts[1])
        and parts[2].casefold() == "statuses"
    ):
        status_id = parts[3]
        canonical_path = f"/users/{parts[1]}/statuses/{status_id}"
    if not _STATUS_ID.fullmatch(status_id):
        raise ValueError("Mastodon status ID is invalid")
    return MastodonStatusTarget(
        instance=host,
        status_id=status_id,
        canonical_url=f"https://{host}{canonical_path}",
    )


@dataclass(frozen=True, slots=True)
class MastodonCommentBudgets:
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
class MastodonCommentScan:
    target: MastodonStatusTarget
    content_external_id: str
    records: tuple[CommentRecord, ...]
    request_count: int
    root_count: int
    child_count: int
    truncated: bool
    provider_id: str = "mastodon_public"


class MastodonCommentProvider(Protocol):
    request_count: int

    async def status(
        self,
        status_id: str,
        *,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]: ...

    async def context(
        self,
        status_id: str,
        *,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]: ...


class MastodonApiCommentProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_requests: int,
        attempts: int = 3,
    ) -> None:
        if not 1 <= max_requests <= 100 or not 1 <= attempts <= 5:
            raise ValueError("Mastodon comment transport limits are invalid")
        self.client = client
        self.max_requests = max_requests
        self.attempts = attempts
        self.request_count = 0

    async def status(
        self,
        status_id: str,
        *,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]:
        return await self._get(
            f"/api/v1/statuses/{status_id}", cancellation=cancellation
        )

    async def context(
        self,
        status_id: str,
        *,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]:
        return await self._get(
            f"/api/v1/statuses/{status_id}/context", cancellation=cancellation
        )

    async def _get(
        self,
        endpoint: str,
        *,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]:
        payload, _response = await mastodon_json(
            self.client,
            endpoint,
            attempts=self.attempts,
            before_request=lambda: self._spend(cancellation),
        )
        if not isinstance(payload, Mapping):
            raise _parse_changed("Mastodon returned an invalid status response.")
        return payload

    def _spend(self, cancellation: CancellationToken) -> None:
        cancellation.raise_if_cancelled()
        if self.request_count >= self.max_requests:
            raise CrawlerFailure(
                CrawlerErrorCode.BUDGET_EXHAUSTED,
                "Mastodon comment request budget was exhausted.",
            )
        self.request_count += 1


@dataclass(frozen=True, slots=True)
class _Node:
    local_id: str
    parent_local_id: str
    external_id: str
    body: str
    author_identity: str
    published_at: Any
    like_count: int
    declared_children: int


class MastodonCommentsAdapter:
    source_id = "mastodon"
    provider_id = "mastodon_public"

    def __init__(
        self,
        provider: MastodonCommentProvider,
        pseudonymizer: IdentityPseudonymizer,
        *,
        fetching_instance: str,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self.fetching_instance = normalize_host(fetching_instance)

    async def crawl(
        self,
        target_value: str,
        budgets: MastodonCommentBudgets,
        *,
        allowed_instances: Iterable[str],
        sort: str = "provider",
        cancellation: CancellationToken | None = None,
    ) -> MastodonCommentScan:
        if sort != "provider":
            raise ValueError(
                "Mastodon context does not expose a selectable sort; use 'provider'."
            )
        token = cancellation or CancellationToken()
        target = parse_mastodon_status_target(
            target_value, allowed_instances=allowed_instances
        )
        if target.instance != self.fetching_instance:
            raise ValueError("Mastodon provider instance does not match the target")

        root = await self.provider.status(target.status_id, cancellation=token)
        if isinstance(root.get("reblog"), Mapping):
            raise _parse_changed("Mastodon target resolved to a boost wrapper.")
        root_value = normalize_status(
            dict(root), fetching_instance=target.instance, discovery={}
        )
        if root_value is None:
            raise _parse_changed("Mastodon root status identity is invalid.")
        content_external_id = str(root_value["external_id"])
        root_local_id = _local_id(root.get("id"))
        if root_local_id != target.status_id:
            raise _parse_changed("Mastodon returned a different root status.")

        context = await self.provider.context(target.status_id, cancellation=token)
        ancestors = context.get("ancestors")
        descendants = context.get("descendants")
        if (
            not isinstance(ancestors, Sequence)
            or isinstance(ancestors, (str, bytes))
            or not isinstance(descendants, Sequence)
            or isinstance(descendants, (str, bytes))
        ):
            raise _parse_changed("Mastodon status context is invalid.")
        if any(not isinstance(item, Mapping) for item in (*ancestors, *descendants)):
            raise _parse_changed("Mastodon status context is invalid.")

        nodes: list[_Node] = []
        local_ids: set[str] = set()
        external_ids: set[str] = set()
        for status in descendants:
            token.raise_if_cancelled()
            assert isinstance(status, Mapping)
            if isinstance(status.get("reblog"), Mapping):
                raise _parse_changed("Mastodon context contained a boost wrapper.")
            local_id = _local_id(status.get("id"))
            parent_id = _local_id(status.get("in_reply_to_id"))
            if local_id == root_local_id or parent_id == local_id:
                raise _parse_changed(
                    "Mastodon context contained a self-referential status."
                )
            if local_id in local_ids:
                raise _parse_changed("Mastodon context contained duplicate status IDs.")
            normalized = normalize_status(
                dict(status), fetching_instance=target.instance, discovery={}
            )
            if normalized is None:
                raise _parse_changed("Mastodon descendant identity is invalid.")
            external_id = str(normalized["external_id"])
            if external_id == content_external_id:
                raise _parse_changed(
                    "Mastodon context repeated the root status identity."
                )
            if external_id in external_ids:
                raise _parse_changed("Mastodon context contained duplicate identities.")
            author_identity = _author_identity(status.get("account"))
            nodes.append(
                _Node(
                    local_id=local_id,
                    parent_local_id=parent_id,
                    external_id=external_id,
                    body=str(normalized["body"])[:4_000],
                    author_identity=author_identity,
                    published_at=normalized["published_at"],
                    like_count=int(normalized["metrics"]["like_count"]),
                    declared_children=int(normalized["metrics"]["comment_count"]),
                )
            )
            local_ids.add(local_id)
            external_ids.add(external_id)

        children: dict[str, list[_Node]] = defaultdict(list)
        for node in nodes:
            children[node.parent_local_id].append(node)

        records: list[CommentRecord] = []
        visited: set[str] = set()
        descendants_by_root: dict[str, int] = {}
        truncated = False

        def add_node(
            node: _Node,
            *,
            parent_external_id: str | None,
            root_external_id: str | None,
            depth: int,
        ) -> None:
            nonlocal truncated
            token.raise_if_cancelled()
            if node.local_id in visited:
                truncated = True
                return
            if len(records) >= budgets.max_total_comments:
                truncated = True
                return
            is_root = parent_external_id is None
            if is_root:
                current_roots = sum(
                    record.parent_external_id is None for record in records
                )
                if current_roots >= budgets.max_root_comments:
                    truncated = True
                    return
                comment_root = node.external_id
                descendants_by_root.setdefault(comment_root, 0)
            else:
                comment_root = root_external_id
                if comment_root is None:
                    raise _parse_changed("Mastodon reply root identity is missing.")
                if (
                    descendants_by_root.get(comment_root, 0)
                    >= budgets.max_children_per_root
                ):
                    truncated = True
                    return
                descendants_by_root[comment_root] = (
                    descendants_by_root.get(comment_root, 0) + 1
                )
            direct_children = children.get(node.local_id, [])
            if node.declared_children > len(direct_children):
                truncated = True
            records.append(
                CommentRecord(
                    source_id="mastodon",
                    external_id=node.external_id,
                    content_external_id=content_external_id,
                    body=node.body,
                    author_pseudonym=self.pseudonymizer.pseudonym(
                        "mastodon", node.author_identity
                    ),
                    published_at=node.published_at,
                    like_count=node.like_count,
                    child_count=node.declared_children,
                    parent_external_id=parent_external_id,
                    root_external_id=comment_root,
                    provenance={
                        "provider_id": "mastodon_public",
                        "contract_version": "cbce.mastodon.comments.v1",
                        "coverage": "bounded_public_status_context",
                        "order": "provider_defined",
                    },
                )
            )
            visited.add(node.local_id)
            if depth >= budgets.max_depth:
                if direct_children:
                    truncated = True
                return
            for child in direct_children:
                add_node(
                    child,
                    parent_external_id=node.external_id,
                    root_external_id=comment_root,
                    depth=depth + 1,
                )

        roots = children.get(root_local_id, [])
        root_declared = int(root_value["metrics"]["comment_count"])
        if root_declared > len(roots):
            truncated = True
        for node in roots:
            add_node(
                node,
                parent_external_id=None,
                root_external_id=None,
                depth=1,
            )
        if len(visited) < len(nodes):
            truncated = True
        root_count = sum(record.parent_external_id is None for record in records)
        return MastodonCommentScan(
            target=target,
            content_external_id=content_external_id,
            records=tuple(records),
            request_count=self.provider.request_count,
            root_count=root_count,
            child_count=len(records) - root_count,
            truncated=truncated,
        )


def _local_id(value: Any) -> str:
    candidate = str(value or "")
    if not _STATUS_ID.fullmatch(candidate):
        raise _parse_changed("Mastodon local status identity is invalid.")
    return candidate


def _author_identity(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise _parse_changed("Mastodon reply author is missing.")
    for field in ("uri", "url"):
        candidate = str(value.get(field) or "")
        try:
            normalized, _host = normalize_target_url(candidate)
        except InvalidTargetUrl:
            continue
        if normalized.startswith("https://"):
            return normalized
    raise _parse_changed("Mastodon reply author identity is invalid.")


def _parse_changed(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)
