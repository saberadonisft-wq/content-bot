import pytest

from app.crawlers.adapters.douyin import DouyinTargetKind, parse_douyin_target
from app.crawlers.adapters.kuaishou import KuaishouTargetKind, parse_kuaishou_target
from app.crawlers.adapters.xhs import XhsTargetKind, parse_xhs_target
from app.crawlers.adapters.zhihu import ZhihuTargetKind, parse_zhihu_target


@pytest.mark.parametrize(
    ("parser", "value", "kind", "identity", "canonical"),
    [
        (
            parse_xhs_target,
            "https://www.xiaohongshu.com/explore/64abcdef0123456789abcdef?xsec_token=drop",
            XhsTargetKind.NOTE,
            "64abcdef0123456789abcdef",
            "https://www.xiaohongshu.com/explore/64abcdef0123456789abcdef",
        ),
        (
            parse_xhs_target,
            "https://www.rednote.com/user/profile/creator_123",
            XhsTargetKind.CREATOR,
            "creator_123",
            "https://www.rednote.com/user/profile/creator_123",
        ),
        (
            parse_douyin_target,
            "https://www.douyin.com/video/7123456789012345678?modal_id=drop",
            DouyinTargetKind.VIDEO,
            "7123456789012345678",
            "https://www.douyin.com/video/7123456789012345678",
        ),
        (
            parse_douyin_target,
            "https://v.douyin.com/AbCdEf/",
            DouyinTargetKind.SHORT_URL,
            "AbCdEf",
            "https://v.douyin.com/AbCdEf/",
        ),
        (
            parse_kuaishou_target,
            "https://www.kuaishou.com/short-video/AbCdEf123",
            KuaishouTargetKind.VIDEO,
            "AbCdEf123",
            "https://www.kuaishou.com/short-video/AbCdEf123",
        ),
        (
            parse_kuaishou_target,
            "https://www.kuaishou.com/profile/creator_123",
            KuaishouTargetKind.CREATOR,
            "creator_123",
            "https://www.kuaishou.com/profile/creator_123",
        ),
        (
            parse_zhihu_target,
            "https://www.zhihu.com/question/123/answer/456?utm=drop",
            ZhihuTargetKind.ANSWER,
            "456",
            "https://www.zhihu.com/question/123/answer/456",
        ),
        (
            parse_zhihu_target,
            "https://zhuanlan.zhihu.com/p/789",
            ZhihuTargetKind.ARTICLE,
            "789",
            "https://zhuanlan.zhihu.com/p/789",
        ),
        (
            parse_zhihu_target,
            "https://www.zhihu.com/zvideo/987",
            ZhihuTargetKind.VIDEO,
            "987",
            "https://www.zhihu.com/zvideo/987",
        ),
    ],
)
def test_browser_platform_target_canonicalization(
    parser, value, kind, identity, canonical
) -> None:
    target = parser(value)
    assert (target.kind, target.external_id, target.canonical_url) == (
        kind,
        identity,
        canonical,
    )


@pytest.mark.parametrize(
    ("parser", "value"),
    [
        (parse_xhs_target, "https://evilxiaohongshu.com/explore/64abcdef0123456789abcdef"),
        (parse_xhs_target, "https://xiaohongshu.com@evil.test/explore/64abcdef0123456789abcdef"),
        (parse_douyin_target, "https://evildouyin.com/video/7123456789012345678"),
        (parse_douyin_target, "https://douyin.com:8443/video/7123456789012345678"),
        (parse_kuaishou_target, "https://evil-kuaishou.com/short-video/AbCdEf123"),
        (parse_kuaishou_target, "https://kuaishou.com@evil.test/profile/creator_123"),
        (parse_zhihu_target, "https://evilzhihu.com/question/123/answer/456"),
        (parse_zhihu_target, "https://zhihu.com:8443/question/123/answer/456"),
    ],
)
def test_browser_platform_targets_reject_hostile_domains(parser, value) -> None:
    with pytest.raises(ValueError):
        parser(value)
