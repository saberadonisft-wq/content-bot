import pytest

from app.crawlers.adapters.tieba import TiebaTargetKind, parse_tieba_target
from app.crawlers.adapters.weibo import WeiboTargetKind, parse_weibo_target
from app.services.channel_scans import normalize_channel


@pytest.mark.parametrize(
    ("value", "kind", "identity", "canonical"),
    [
        (
            "https://m.weibo.cn/detail/1234567890?jumpfrom=weibocom",
            WeiboTargetKind.POST,
            "1234567890",
            "https://m.weibo.cn/detail/1234567890",
        ),
        (
            "https://weibo.com/1234567890/AbCdEf12",
            WeiboTargetKind.POST,
            "AbCdEf12",
            "https://m.weibo.cn/detail/AbCdEf12",
        ),
        (
            "https://weibo.com/u/1234567890",
            WeiboTargetKind.CREATOR,
            "1234567890",
            "https://weibo.com/u/1234567890",
        ),
    ],
)
def test_weibo_target_canonicalization(value, kind, identity, canonical) -> None:
    target = parse_weibo_target(value)
    assert (target.kind, target.external_id, target.canonical_url) == (
        kind,
        identity,
        canonical,
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://evilweibo.com/u/123",
        "https://weibo.com@evil.test/u/123",
        "https://weibo.com:8443/u/123",
        "https://weibo.com/login.php",
    ],
)
def test_weibo_target_rejects_hostile_or_unsupported_values(value) -> None:
    with pytest.raises(ValueError):
        parse_weibo_target(value)


@pytest.mark.parametrize(
    ("value", "kind", "identity", "canonical"),
    [
        (
            "https://tieba.baidu.com/p/1234567890?pn=2",
            TiebaTargetKind.THREAD,
            "1234567890",
            "https://tieba.baidu.com/p/1234567890",
        ),
        (
            "https://tieba.baidu.com/f?kw=game%20news&ie=utf-8",
            TiebaTargetKind.FORUM,
            "game news",
            "https://tieba.baidu.com/f?kw=game%20news",
        ),
        (
            "https://tieba.baidu.com/home/main?id=creator-token",
            TiebaTargetKind.CREATOR,
            "creator-token",
            "https://tieba.baidu.com/home/main?id=creator-token",
        ),
    ],
)
def test_tieba_target_canonicalization(value, kind, identity, canonical) -> None:
    target = parse_tieba_target(value)
    assert (target.kind, target.external_id, target.canonical_url) == (
        kind,
        identity,
        canonical,
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://eviltieba.baidu.com/p/123",
        "https://tieba.baidu.com@evil.test/p/123",
        "https://tieba.baidu.com:8443/p/123",
        "https://tieba.baidu.com/f?kw=",
    ],
)
def test_tieba_target_rejects_hostile_or_unsupported_values(value) -> None:
    with pytest.raises(ValueError):
        parse_tieba_target(value)


def test_tieba_forum_channel_preserves_canonical_keyword_query() -> None:
    channel = normalize_channel(
        {"url": "http://www.tieba.baidu.com/f?kw=game&utm_source=ignored"}
    )
    assert channel["source_id"] == "tieba"
    assert channel["normalized_url"] == "https://tieba.baidu.com/f?kw=game"


def test_tieba_thread_cannot_be_saved_as_a_channel() -> None:
    with pytest.raises(ValueError, match="not channels"):
        normalize_channel({"url": "https://tieba.baidu.com/p/1234567890"})
