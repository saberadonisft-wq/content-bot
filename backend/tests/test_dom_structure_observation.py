import asyncio

import pytest

from app.crawlers.runtime import observe_dom_structure


class Page:
    def __init__(self, url="https://s.weibo.com/weibo") -> None:
        self.url = url

    async def evaluate(self, script, argument):
        assert "innerText" not in script
        assert "textContent" not in script
        assert ".value" not in script
        assert argument["link_path_prefixes"] == {"content": "/detail/"}
        assert argument["sentinel_selectors"] == {"challenge": "div.unhuman"}
        return {
            "element_count": 15,
            "open_shadow_root_count": 1,
            "landmarks": [
                ["div", 10],
                ["div.card-wrap", 4],
                ["a.user-1234567890123456", 1],
                ["span.ABCDEFGHIJKLMNOPQRSTUVWXYZ", 1],
                ["div.card-wrap", 4],
            ],
            "attribute_names": [
                ["class", 12],
                ["data-testid", 3],
                ["bad.attribute", 1],
            ],
            "link_path_counts": {"content": 3},
            "sentinel_counts": {"challenge": 0},
        }


def test_dom_observation_keeps_only_bounded_value_free_structure() -> None:
    observation = asyncio.run(
        observe_dom_structure(
            Page(),
            source_id="weibo",
            provider_id="cbce_weibo",
            operation="search",
            allowed_hosts=("weibo.com", "weibo.cn"),
            link_path_prefixes={"content": "/detail/"},
            sentinel_selectors={"challenge": "div.unhuman"},
        )
    )

    assert observation.final_host == "s.weibo.com"
    assert [(item.signature, item.count) for item in observation.landmarks] == [
        ("div", 10),
        ("div.card-wrap", 4),
    ]
    assert [item.signature for item in observation.attribute_names] == [
        "class",
        "data-testid",
    ]
    assert "url" not in observation.as_dict()
    assert observation.as_dict()["link_path_counts"] == [
        {"signature": "content", "count": 3}
    ]
    assert observation.as_dict()["sentinel_counts"] == [
        {"signature": "challenge", "count": 0}
    ]


def test_dom_observation_rejects_hostile_domain_boundary() -> None:
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(
            observe_dom_structure(
                Page("https://evilweibo.com/weibo"),
                source_id="weibo",
                provider_id="cbce_weibo",
                operation="search",
                allowed_hosts=("weibo.com",),
                link_path_prefixes={"content": "/detail/"},
                sentinel_selectors={"challenge": "div.unhuman"},
            )
        )


@pytest.mark.parametrize("field", ["element_count", "open_shadow_root_count"])
def test_dom_observation_rejects_unbounded_counts(field) -> None:
    class InvalidPage(Page):
        async def evaluate(self, script, argument):
            values = await super().evaluate(script, argument)
            values[field] = 100_001
            return values

    with pytest.raises(ValueError, match="bound"):
        asyncio.run(
            observe_dom_structure(
                InvalidPage(),
                source_id="weibo",
                provider_id="cbce_weibo",
                operation="search",
                allowed_hosts=("weibo.com",),
                link_path_prefixes={"content": "/detail/"},
                sentinel_selectors={"challenge": "div.unhuman"},
            )
        )


def test_dom_observation_rejects_unsafe_path_contract() -> None:
    with pytest.raises(ValueError, match="prefix"):
        asyncio.run(
            observe_dom_structure(
                Page(),
                source_id="weibo",
                provider_id="cbce_weibo",
                operation="search",
                allowed_hosts=("weibo.com",),
                link_path_prefixes={"private.value": "/detail/?token="},
                sentinel_selectors={"challenge": "div.unhuman"},
            )
        )


def test_dom_observation_rejects_unsafe_sentinel_selector() -> None:
    with pytest.raises(ValueError, match="sentinel"):
        asyncio.run(
            observe_dom_structure(
                Page(),
                source_id="weibo",
                provider_id="cbce_weibo",
                operation="search",
                allowed_hosts=("weibo.com",),
                link_path_prefixes={"content": "/detail/"},
                sentinel_selectors={"challenge": "div[data-token]"},
            )
        )
