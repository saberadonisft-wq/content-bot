from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crawlers.adapters.mastodon import (
    MastodonApiCommentProvider,
    MastodonCommentBudgets,
    MastodonCommentsAdapter,
    parse_mastodon_status_target,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)
from app.services.mastodon_api import mastodon_external_id


def status(
    local_id: str,
    *,
    username: str,
    parent_id: str | None = None,
    replies: int = 0,
) -> dict:
    return {
        "id": local_id,
        "uri": f"https://mastodon.social/users/{username}/statuses/{local_id}",
        "url": f"https://mastodon.social/@{username}/{local_id}",
        "content": f"<p>body-{local_id}</p>",
        "created_at": "2026-08-01T08:00:00Z",
        "in_reply_to_id": parent_id,
        "favourites_count": 2,
        "replies_count": replies,
        "reblogs_count": 0,
        "account": {
            "uri": f"https://mastodon.social/users/{username}",
            "url": f"https://mastodon.social/@{username}",
            "acct": username,
            "id": "not-retained",
        },
    }


class FakeContextProvider:
    def __init__(self, root: dict, descendants: list[dict]) -> None:
        self.root = root
        self.descendants = descendants
        self.request_count = 0
        self.calls = []

    async def status(self, status_id, *, cancellation):
        cancellation.raise_if_cancelled()
        self.request_count += 1
        self.calls.append(("status", status_id))
        return self.root

    async def context(self, status_id, *, cancellation):
        cancellation.raise_if_cancelled()
        self.request_count += 1
        self.calls.append(("context", status_id))
        return {"ancestors": [], "descendants": self.descendants}


def test_mastodon_status_target_requires_exact_configured_instance() -> None:
    target = parse_mastodon_status_target(
        "https://Mastodon.Social/@player/100?ref=ignored",
        allowed_instances=("mastodon.social",),
    )
    assert target.instance == "mastodon.social"
    assert target.status_id == "100"
    assert target.canonical_url == "https://mastodon.social/@player/100"
    activity_target = parse_mastodon_status_target(
        "https://mastodon.social/users/player/statuses/100",
        allowed_instances=("mastodon.social",),
    )
    assert activity_target.status_id == "100"


@pytest.mark.parametrize(
    "value",
    [
        "http://mastodon.social/@player/100",
        "https://evilmastodon.social/@player/100",
        "https://user@mastodon.social/@player/100",
        "https://mastodon.social/@player/%31%30%30",
        "https://mastodon.social/@player",
        "https://mastodon.social/api/v1/statuses/100",
    ],
)
def test_mastodon_status_target_rejects_unsafe_or_nonpublic_paths(value: str) -> None:
    with pytest.raises(ValueError):
        parse_mastodon_status_target(
            value, allowed_instances=("mastodon.social",)
        )


def test_mastodon_context_reconstructs_hierarchy_and_exact_budgets() -> None:
    root = status("100", username="content", replies=3)
    root1 = status("101", username="one", parent_id="100", replies=1)
    child1 = status("102", username="two", parent_id="101", replies=0)
    root2 = status("103", username="three", parent_id="100", replies=0)
    root3 = status("104", username="four", parent_id="100", replies=0)
    provider = FakeContextProvider(
        root,
        [child1, root1, root2, root3],
    )
    adapter = MastodonCommentsAdapter(
        provider,
        IdentityPseudonymizer(b"m" * 32),
        fetching_instance="mastodon.social",
    )
    scan = asyncio.run(
        adapter.crawl(
            "https://mastodon.social/@content/100",
            MastodonCommentBudgets(
                max_root_comments=2,
                max_children_per_root=1,
                max_total_comments=3,
                max_requests=2,
                max_depth=3,
            ),
            allowed_instances=("mastodon.social",),
        )
    )

    content_id = mastodon_external_id(root["uri"])
    root1_id = mastodon_external_id(root1["uri"])
    child1_id = mastodon_external_id(child1["uri"])
    root2_id = mastodon_external_id(root2["uri"])
    assert scan.content_external_id == content_id
    assert [record.external_id for record in scan.records] == [
        root1_id,
        child1_id,
        root2_id,
    ]
    assert scan.records[0].parent_external_id is None
    assert scan.records[0].root_external_id == root1_id
    assert scan.records[1].parent_external_id == root1_id
    assert scan.records[1].root_external_id == root1_id
    assert scan.records[2].parent_external_id is None
    assert all(record.content_external_id == content_id for record in scan.records)
    assert scan.root_count == 2
    assert scan.child_count == 1
    assert scan.request_count == 2
    assert scan.truncated is True
    assert "not-retained" not in repr(scan.records)
    assert "https://mastodon.social/users/" not in repr(scan.records)
    assert all(record.author_pseudonym.startswith("mastodon_") for record in scan.records)
    assert provider.calls == [("status", "100"), ("context", "100")]


def test_mastodon_context_orphan_is_omitted_and_marks_partial() -> None:
    root = status("100", username="content")
    orphan = status("102", username="two", parent_id="missing")
    provider = FakeContextProvider(root, [orphan])
    adapter = MastodonCommentsAdapter(
        provider,
        IdentityPseudonymizer(b"m" * 32),
        fetching_instance="mastodon.social",
    )
    scan = asyncio.run(
        adapter.crawl(
            "https://mastodon.social/@content/100",
            MastodonCommentBudgets(),
            allowed_instances=("mastodon.social",),
        )
    )
    assert scan.records == ()
    assert scan.truncated is True


def test_mastodon_context_rejects_self_referential_descendant() -> None:
    root = status("100", username="content")
    self_reply = status("101", username="loop", parent_id="101")
    provider = FakeContextProvider(root, [self_reply])
    adapter = MastodonCommentsAdapter(
        provider,
        IdentityPseudonymizer(b"m" * 32),
        fetching_instance="mastodon.social",
    )
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            adapter.crawl(
                "https://mastodon.social/@content/100",
                MastodonCommentBudgets(),
                allowed_instances=("mastodon.social",),
            )
        )
    assert raised.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_mastodon_root_identity_is_verified_from_status_endpoint() -> None:
    provider = FakeContextProvider(status("different", username="content"), [])
    adapter = MastodonCommentsAdapter(
        provider,
        IdentityPseudonymizer(b"m" * 32),
        fetching_instance="mastodon.social",
    )
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            adapter.crawl(
                "https://mastodon.social/@content/100",
                MastodonCommentBudgets(),
                allowed_instances=("mastodon.social",),
            )
        )
    assert raised.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_mastodon_transport_counts_physical_retry_attempts(monkeypatch) -> None:
    class RetryClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, endpoint, params=None):
            del endpoint, params
            self.calls += 1
            return httpx.Response(503, json={})

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.mastodon_api.asyncio.sleep", no_sleep)
    client = RetryClient()
    provider = MastodonApiCommentProvider(client, max_requests=1, attempts=3)
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(provider.status("100", cancellation=CancellationToken()))
    assert raised.value.code is CrawlerErrorCode.BUDGET_EXHAUSTED
    assert provider.request_count == 1
    assert client.calls == 1


def test_mastodon_transport_uses_documented_status_and_context_paths() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = []

        async def get(self, endpoint, params=None):
            self.calls.append((endpoint, params))
            return httpx.Response(200, json={})

    client = FakeClient()
    provider = MastodonApiCommentProvider(client, max_requests=2, attempts=1)

    async def invoke() -> None:
        await provider.status("100", cancellation=CancellationToken())
        await provider.context("100", cancellation=CancellationToken())

    asyncio.run(invoke())
    assert client.calls == [
        ("/api/v1/statuses/100", None),
        ("/api/v1/statuses/100/context", None),
    ]
