from unittest.mock import MagicMock

from app.services.runs import FAST_SOURCES, EventBus, RunManager


def test_fast_sources_membership() -> None:
    assert "steam" in FAST_SOURCES
    assert "web" in FAST_SOURCES
    assert "bluesky" in FAST_SOURCES
    assert "mastodon" in FAST_SOURCES
    assert "youtube" not in FAST_SOURCES


def test_tiered_semaphores_exist() -> None:
    manager = RunManager({}, EventBus(), MagicMock())
    assert manager._fast_semaphore._value == 8
    assert manager._quota_semaphore._value == 3
    assert manager._browser_semaphore._value == 1
    assert manager._semaphore is manager._quota_semaphore

