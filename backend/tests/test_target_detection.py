import pytest

from app.crawlers import SOURCE_REGISTRY
from app.crawlers.target_detection import InvalidTargetUrl
from app.services.channel_scans import detect_source_id, normalize_channel


@pytest.mark.parametrize(
    ("url", "source_id"),
    [
        ("https://m.youtube.com/@game", "youtube"),
        ("https://redd.it/abc", "reddit"),
        ("https://m.bilibili.com/video/BV1x", "bilibili"),
        ("https://b23.tv/abcd", "bilibili"),
        ("https://tieba.baidu.com/f?kw=game", "tieba"),
        ("https://m.weibo.cn/u/1", "weibo"),
        ("https://www.rednote.com/explore/64abcdef0123456789abcdef", "xhs"),
        ("https://www.tiktok.com/@game", "tiktok"),
        ("https://fb.watch/demo", "facebook"),
    ],
)
def test_target_detection_covers_mobile_short_and_subdomains(url: str, source_id: str) -> None:
    assert detect_source_id(url) == source_id


@pytest.mark.parametrize(
    "url",
    [
        "https://evilreddit.com/r/game",
        "https://notweibo.com/u/1",
        "https://example.com/@not-mastodon",
    ],
)
def test_hostile_near_matches_are_not_claimed_as_social_sources(url: str) -> None:
    assert detect_source_id(url) == "web"


def test_unknown_web_url_is_saved_as_manual_not_feed_scannable() -> None:
    channel = normalize_channel({"url": "https://example.com/@someone"})
    assert channel["source_id"] == "web"
    assert channel["mode"] == "manual"


def test_steam_saved_channel_is_canonical_app_id_pin() -> None:
    channel = normalize_channel(
        {"url": "https://steamcommunity.com/games/42/?tracking=ignored"}
    )
    assert channel["source_id"] == "steam"
    assert channel["mode"] == "public"
    assert channel["normalized_url"] == "https://store.steampowered.com/app/42/"


def test_non_app_steam_url_is_not_a_scannable_channel() -> None:
    with pytest.raises(ValueError, match="App ID"):
        normalize_channel({"url": "https://store.steampowered.com/search/?term=game"})


def test_bluesky_saved_channel_is_one_canonical_profile() -> None:
    channel = normalize_channel(
        {"url": "https://bsky.app/profile/Player.Bsky.Social?tracking=ignored"}
    )
    assert channel["source_id"] == "bluesky"
    assert channel["normalized_url"] == "https://bsky.app/profile/player.bsky.social"


def test_bluesky_post_url_cannot_be_saved_as_channel() -> None:
    with pytest.raises(ValueError, match="profile"):
        normalize_channel(
            {"url": "https://bsky.app/profile/player.bsky.social/post/abc123"}
        )


def test_mastodon_saved_channel_requires_configured_profile_origin() -> None:
    channel = normalize_channel(
        {"url": "https://mastodon.social/@Player?tracking=ignored"}
    )
    assert channel["source_id"] == "mastodon"
    assert channel["mode"] == "public"
    assert channel["normalized_url"] == "https://mastodon.social/@player"


def test_mastodon_status_url_cannot_be_saved_as_channel() -> None:
    with pytest.raises(ValueError, match="profile"):
        normalize_channel({"url": "https://mastodon.social/@player/123"})


def test_unconfigured_mastodon_like_host_is_not_auto_detected() -> None:
    channel = normalize_channel({"url": "https://example.social/@player"})
    assert channel["source_id"] == "web"
    assert channel["mode"] == "manual"


def test_url_userinfo_is_rejected() -> None:
    with pytest.raises(InvalidTargetUrl):
        SOURCE_REGISTRY.match_target("https://user:secret@youtube.com/@game")
