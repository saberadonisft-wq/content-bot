import pytest

from app.crawlers.adapters.facebook import FacebookTargetKind, parse_facebook_target
from app.crawlers.adapters.instagram import InstagramTargetKind, parse_instagram_target
from app.crawlers.adapters.tiktok import TikTokTargetKind, parse_tiktok_target


@pytest.mark.parametrize(
    ("parser", "value", "kind", "identity"),
    [
        (
            parse_tiktok_target,
            "https://www.tiktok.com/@game.dev/video/7123456789012345678?lang=en",
            TikTokTargetKind.VIDEO,
            "7123456789012345678",
        ),
        (
            parse_tiktok_target,
            "https://vm.tiktok.com/AbCdEf/",
            TikTokTargetKind.SHORT_URL,
            "AbCdEf",
        ),
        (
            parse_facebook_target,
            "https://www.facebook.com/GamePage/posts/1234567890/?ref=drop",
            FacebookTargetKind.CONTENT,
            "1234567890",
        ),
        (
            parse_facebook_target,
            "https://fb.watch/AbCdEf/",
            FacebookTargetKind.SHORT_URL,
            "AbCdEf",
        ),
        (
            parse_instagram_target,
            "https://www.instagram.com/reel/AbCdEf12/?utm=drop",
            InstagramTargetKind.MEDIA,
            "AbCdEf12",
        ),
        (
            parse_instagram_target,
            "https://www.instagram.com/game.studio/",
            InstagramTargetKind.ACCOUNT,
            "game.studio",
        ),
    ],
)
def test_external_platform_target_canonicalization(parser, value, kind, identity) -> None:
    target = parser(value)
    assert target.kind is kind
    assert target.external_id == identity


@pytest.mark.parametrize(
    ("parser", "value"),
    [
        (parse_tiktok_target, "https://eviltiktok.com/@game/video/7123456789012345678"),
        (parse_tiktok_target, "https://tiktok.com@evil.test/@game/video/7123456789012345678"),
        (parse_facebook_target, "https://evilfacebook.com/GamePage/posts/1234567890"),
        (parse_facebook_target, "https://facebook.com:8443/GamePage/posts/1234567890"),
        (parse_instagram_target, "https://evilinstagram.com/reel/AbCdEf12"),
        (parse_instagram_target, "https://instagram.com@evil.test/reel/AbCdEf12"),
    ],
)
def test_external_platform_targets_reject_hostile_domains(parser, value) -> None:
    with pytest.raises(ValueError):
        parser(value)
