from __future__ import annotations

import pytest

from scripts.acquisition_batch_canary import _is_bilibili_target


@pytest.mark.parametrize(
    "target",
    [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://space.bilibili.com/2",
        "https://www.bilibili.tv/video/BV1xx411c7mD",
        "https://b23.tv/abc123",
    ],
)
def test_connection_canary_accepts_bilibili_hosts(target):
    assert _is_bilibili_target(target)


@pytest.mark.parametrize(
    "target",
    [
        "https://www.youtube.com/@GoogleDevelopers/videos",
        "https://bilibili.com.evil.example/video/BV1xx411c7mD",
        "not a url",
    ],
)
def test_connection_canary_rejects_non_bilibili_hosts(target):
    assert not _is_bilibili_target(target)
