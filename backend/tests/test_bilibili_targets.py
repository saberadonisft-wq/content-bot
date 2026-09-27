import pytest

from app.crawlers.adapters.bilibili import (
    BilibiliTargetKind,
    parse_bilibili_target,
)
from app.crawlers.adapters.bilibili.targets import BilibiliTargetError


@pytest.mark.parametrize(
    ("value", "kind", "external_id", "canonical", "part"),
    [
        (
            "BV1ab411c7De",
            BilibiliTargetKind.VIDEO,
            "BV1ab411c7De",
            "https://www.bilibili.com/video/BV1ab411c7De",
            None,
        ),
        (
            "https://m.bilibili.com/video/av123?p=2&utm_source=test",
            BilibiliTargetKind.VIDEO,
            "av123",
            "https://www.bilibili.com/video/av123",
            2,
        ),
        (
            "https://space.bilibili.com/123/video",
            BilibiliTargetKind.CREATOR,
            "123",
            "https://space.bilibili.com/123",
            None,
        ),
        (
            "https://space.bilibili.com/123/upload/video",
            BilibiliTargetKind.CREATOR,
            "123",
            "https://space.bilibili.com/123",
            None,
        ),
        (
            "https://www.bilibili.com/medialist/play/ml1103407912?from=share",
            BilibiliTargetKind.PLAYLIST,
            "ml1103407912",
            "https://www.bilibili.com/medialist/detail/ml1103407912",
            None,
        ),
        (
            "https://space.bilibili.com/123/favlist?fid=1103407912&ftype=create",
            BilibiliTargetKind.PLAYLIST,
            "ml1103407912",
            "https://www.bilibili.com/medialist/detail/ml1103407912",
            None,
        ),
        (
            "https://t.bilibili.com/456",
            BilibiliTargetKind.DYNAMIC,
            "456",
            "https://t.bilibili.com/456",
            None,
        ),
        (
            "https://www.bilibili.com/opus/789",
            BilibiliTargetKind.DYNAMIC,
            "789",
            "https://www.bilibili.com/opus/789",
            None,
        ),
    ],
)
def test_parse_bilibili_targets(value, kind, external_id, canonical, part) -> None:
    target = parse_bilibili_target(value)
    assert target.kind is kind
    assert target.external_id == external_id
    assert target.canonical_url == canonical
    assert target.part_index == part
    assert target.requires_resolution is False


def test_short_url_is_explicitly_unresolved() -> None:
    target = parse_bilibili_target("https://b23.tv/AbC_12?ignored=1")
    assert target.kind is BilibiliTargetKind.SHORT_URL
    assert target.external_id is None
    assert target.requires_resolution is True
    assert target.canonical_url == "https://b23.tv/AbC_12"


@pytest.mark.parametrize(
    "value",
    [
        "https://evilbilibili.com/video/BV1ab411c7De",
        "https://bilibili.com.evil.test/video/BV1ab411c7De",
        "https://user:pass@www.bilibili.com/video/BV1ab411c7De",
        "https://www.bilibili.com:444/video/BV1ab411c7De",
        "https://www.bilibili.com/video/BV1bad",
        "https://www.bilibili.com/video/av123?p=zero",
        "https://www.bilibili.com/search?keyword=game",
        "https://www.bilibili.com/medialist/detail/ml0",
        "https://space.bilibili.com/123/favlist?fid=not-a-number",
        "123",
    ],
)
def test_parse_bilibili_target_rejects_ambiguous_or_hostile_inputs(value) -> None:
    with pytest.raises(BilibiliTargetError):
        parse_bilibili_target(value)
