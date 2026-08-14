from pathlib import Path

import pytest

from app.crawlers import SOURCE_REGISTRY
from app.crawlers.login_session import LOGIN_HOMEPAGES, login_homepage
from app.crawlers.runtime import ProfileNamespace


def test_login_bootstrap_covers_all_seven_mediacrawler_browser_sources() -> None:
    assert tuple(LOGIN_HOMEPAGES) == (
        "xhs",
        "douyin",
        "kuaishou",
        "bilibili",
        "weibo",
        "tieba",
        "zhihu",
    )
    assert login_homepage("bili") == "https://www.bilibili.com/"
    assert login_homepage("weibo") == "https://s.weibo.com/"


def test_login_bootstrap_rejects_non_browser_source() -> None:
    with pytest.raises(ValueError, match="does not use"):
        login_homepage("x")


def test_login_bootstrap_uses_shared_default_account_profile(tmp_path: Path) -> None:
    namespace = ProfileNamespace(tmp_path / "profiles-v2", SOURCE_REGISTRY)
    profile = namespace.profile("weibo", "default")
    repeated = namespace.profile("weibo", "default")

    assert profile.source_id == "weibo"
    assert len(profile.account_key) == 24
    assert profile == repeated
    assert profile.path == (
        tmp_path / "profiles-v2" / "weibo" / profile.account_key
    ).resolve()
