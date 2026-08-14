import asyncio

import httpx
import pytest

from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.mastodon_api import (
    MastodonRequestBudget,
    html_text,
    mastodon_external_id,
    mastodon_instances,
    mastodon_json,
    next_max_id,
    normalize_hashtag,
    normalize_status,
)


class Response:
    def __init__(self, payload, *, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get(self, endpoint, params=None):
        del endpoint, params
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_instance_config_and_hashtag_are_bounded() -> None:
    assert mastodon_instances('["Mastodon.Social", "https://dice.camp/"]') == (
        "mastodon.social",
        "dice.camp",
    )
    assert normalize_hashtag(" #Black Myth: Wukong! ") == "blackmythwukong"
    with pytest.raises(ValueError, match="HTTPS"):
        mastodon_instances("http://mastodon.social")
    with pytest.raises(ValueError, match="private"):
        mastodon_instances("127.0.0.1")


def test_html_parser_decodes_entities_and_preserves_blocks() -> None:
    assert html_text("<p>Hello &amp; <strong>world</strong></p><p>Next<br>line</p>") == (
        "Hello & world\n\nNext\nline"
    )


def test_remote_copies_and_edits_keep_origin_identity() -> None:
    post = {
        "id": "local-copy-1",
        "uri": "https://origin.example/users/player/statuses/42",
        "url": "https://origin.example/@player/42",
        "content": "<p>Version one</p>",
        "created_at": "2026-08-13T01:00:00Z",
        "edited_at": "2026-08-13T02:00:00Z",
        "account": {"acct": "player@origin.example", "id": "private-local-id"},
    }
    left = normalize_status(post, fetching_instance="one.example", discovery={})
    right = normalize_status(
        {**post, "id": "different-copy", "content": "<p>Edited</p>"},
        fetching_instance="two.example",
        discovery={},
    )
    assert left and right
    assert left["external_id"] == right["external_id"] == mastodon_external_id(post["uri"])
    assert left["raw_payload"]["content_version"] == "2026-08-13T02:00:00Z"
    assert "account" not in left["raw_payload"]
    assert "private-local-id" not in repr(left)


def test_reblog_reply_and_media_normalize_to_original() -> None:
    original = {
        "id": "original-local-id",
        "uri": "https://origin.example/users/player/statuses/42",
        "url": "https://origin.example/@player/42",
        "content": "<p>Original</p>",
        "created_at": "2026-08-13T01:00:00Z",
        "in_reply_to_id": "parent-local-id",
        "favourites_count": 3,
        "replies_count": 2,
        "reblogs_count": 1,
        "account": {"acct": "player@origin.example"},
        "media_attachments": [
            {
                "type": "image",
                "url": "https://cdn.origin.example/original.jpg",
                "preview_url": "https://cdn.origin.example/preview.jpg",
                "description": "alt text",
            }
        ],
    }
    normalized = normalize_status(
        {"id": "boost-wrapper", "reblog": original},
        fetching_instance="fetch.example",
        discovery={"hashtag": "game"},
    )
    assert normalized
    assert normalized["canonical_url"] == original["url"]
    assert normalized["raw_payload"]["is_reblog"] is True
    assert normalized["raw_payload"]["is_reply"] is True
    assert normalized["raw_payload"]["reply_local_id"].startswith("mastodon:")
    assert normalized["raw_payload"]["media"] == [
        {
            "kind": "image",
            "url": "https://cdn.origin.example/original.jpg",
            "preview_url": "https://cdn.origin.example/preview.jpg",
            "description": "alt text",
        }
    ]


def test_link_header_is_the_only_pagination_authority() -> None:
    response = Response(
        [],
        headers={
            "Link": '<https://mastodon.social/api/v1/timelines/tag/game?max_id=older>; rel="next", '
            '<https://mastodon.social/api/v1/timelines/tag/game?min_id=newer>; rel="prev"'
        },
    )
    assert next_max_id(response, [{"id": "fallback"}]) == "older"
    assert next_max_id(Response([]), [{"id": "fallback"}]) is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, CrawlerErrorCode.AUTH_REQUIRED),
        (403, CrawlerErrorCode.PERMISSION_REQUIRED),
        (404, CrawlerErrorCode.NOT_FOUND),
        (429, CrawlerErrorCode.RATE_LIMITED),
        (503, CrawlerErrorCode.TRANSPORT_ERROR),
    ],
)
def test_transport_errors_are_typed(status, code) -> None:
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            mastodon_json(
                Client([Response({}, status_code=status)]),
                "/api/v2/instance",
                attempts=1,
            )
        )
    assert raised.value.code is code
    assert "secret" not in raised.value.safe_message


def test_transport_retries_and_budget_is_per_logical_request(monkeypatch) -> None:
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.mastodon_api.asyncio.sleep", no_sleep)
    request = httpx.Request("GET", "https://mastodon.social/api/v2/instance")
    client = Client(
        [
            httpx.ConnectError("down", request=request),
            Response({"domain": "mastodon.social"}),
        ]
    )
    budget = MastodonRequestBudget(1)
    payload, _response = asyncio.run(
        mastodon_json(client, "/api/v2/instance", budget=budget)
    )
    assert payload["domain"] == "mastodon.social"
    assert client.calls == 2
    assert budget.remaining == 0
