import json
from datetime import UTC, datetime

import pytest

from app.crawlers.runtime import sanitize_http_observation


def test_contract_observation_keeps_shape_but_no_values_or_secret_fields() -> None:
    observation = sanitize_http_observation(
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="search",
        method="GET",
        url=(
            "https://api.bilibili.com/x/search/123456/BV1ab411c7De"
            "?keyword=private-game&access_token=very-secret&cursor=opaque-value"
        ),
        allowed_hosts=("bilibili.com",),
        status=200,
        resource_type="xhr",
        content_type="application/json; charset=utf-8",
        request_headers={
            "Authorization": "Bearer abcdefghijklmnop",
            "Cookie": "session=secret",
            "Accept": "application/json",
            "X-Trace": "private-trace-value",
        },
        response_headers={
            "Set-Cookie": "session=secret",
            "Content-Type": "application/json",
        },
        json_value={
            "data": {
                "title": "private title",
                "creator_id": "raw-uid-123",
                "access_token": "very-secret",
                "items": [{"id": 987, "published": True}],
            }
        },
        observed_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    encoded = json.dumps(observation.as_dict(), sort_keys=True)

    assert observation.url_pattern == (
        "https://api.bilibili.com/x/search/{numeric_id}/{video_id}"
    )
    assert observation.query_names == ("cursor", "keyword", "redacted_field")
    assert observation.request_header_names == ("accept", "x-trace")
    assert observation.response_header_names == ("content-type",)
    assert observation.json_shape["data"]["title"] == "string"
    assert observation.json_shape["data"]["items"] == [
        {"id": "integer", "published": "boolean"}
    ]
    for secret in (
        "private-game",
        "very-secret",
        "opaque-value",
        "abcdefghijklmnop",
        "session=secret",
        "private-trace-value",
        "private title",
        "raw-uid-123",
    ):
        assert secret not in encoded


@pytest.mark.parametrize(
    "url",
    [
        "https://evilbilibili.com/x/search",
        "https://bilibili.com.evil.test/x/search",
        "https://user:pass@api.bilibili.com/x/search",
        "https://api.bilibili.com:444/x/search",
        "file:///tmp/response.json",
    ],
)
def test_contract_observation_rejects_out_of_scope_hosts(url) -> None:
    with pytest.raises(ValueError):
        sanitize_http_observation(
            source_id="bilibili",
            provider_id="cbce_bilibili",
            operation="search",
            method="GET",
            url=url,
            allowed_hosts=("bilibili.com",),
            status=200,
            resource_type="xhr",
            content_type="application/json",
            request_headers={},
            response_headers={},
        )
