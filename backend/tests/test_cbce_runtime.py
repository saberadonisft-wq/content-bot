import pytest

from app.config import settings
from app.services.cbce_runtime import (
    build_cbce_supervisor,
    cbce_browser_preflight,
    cbce_provider_rollout_status,
    provider_overrides,
)


def test_cbce_runtime_is_off_by_default_and_does_not_replace_bridge(monkeypatch) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", False)
    assert build_cbce_supervisor() is None
    assert cbce_provider_rollout_status("tieba", "cbce_tieba", "search") == {
        "ready": False,
        "reason_code": "CBCE_FEATURE_DISABLED",
        "detail": "Enable the clean-room crawler runtime before using this provider.",
    }


def test_cbce_runtime_builds_only_after_flag_and_validates_overrides(monkeypatch) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(settings, "content_bot_cbce_event_queue_size", 7)
    monkeypatch.setattr(settings, "content_bot_cbce_cleanup_timeout_seconds", 3)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"dy":{"search":"legacy_bridge"},"x":{"render_embed":"x_embed"}}',
    )
    supervisor = build_cbce_supervisor()
    assert supervisor is not None
    assert supervisor.event_queue_capacity == 7
    assert provider_overrides() == {
        "douyin": {"search": "legacy_bridge"},
        "x": {"render_embed": "x_embed"},
    }


def test_tieba_clean_room_search_is_an_explicit_valid_override() -> None:
    assert provider_overrides('{"tieba":{"search":"cbce_tieba"}}') == {
        "tieba": {"search": "cbce_tieba"}
    }


def test_bilibili_clean_room_search_is_an_explicit_valid_override() -> None:
    assert provider_overrides('{"bilibili":{"search":"cbce_bilibili"}}') == {
        "bilibili": {"search": "cbce_bilibili"}
    }


@pytest.mark.parametrize("source_id", ["xhs", "douyin", "kuaishou", "weibo", "zhihu"])
def test_reviewed_dom_search_is_an_explicit_valid_override(source_id: str) -> None:
    provider_id = f"cbce_{source_id}"
    assert provider_overrides(
        f'{{"{source_id}":{{"search":"{provider_id}"}}}}'
    ) == {source_id: {"search": provider_id}}


def test_cbce_operation_rollout_requires_source_and_operation_selection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(settings, "content_bot_cbce_provider_overrides", "{}")
    monkeypatch.setattr(
        "app.services.cbce_runtime.cbce_browser_preflight",
        lambda: {"ready": True, "reason_code": None},
    )
    search = cbce_provider_rollout_status("tieba", "cbce_tieba", "search")
    detail = cbce_provider_rollout_status(
        "tieba", "cbce_tieba", "fetch_detail"
    )
    assert search and search["reason_code"] == "PROVIDER_NOT_SELECTED"
    assert detail and detail["reason_code"] == "PROVIDER_NOT_SELECTED"

    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"tieba":{"fetch_detail":"cbce_tieba"}}',
    )
    detail = cbce_provider_rollout_status(
        "tieba", "cbce_tieba", "fetch_detail"
    )
    assert detail and detail["reason_code"] == "SOURCE_PROVIDER_NOT_SELECTED"

    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"tieba":{"search":"cbce_tieba","fetch_detail":"cbce_tieba"}}',
    )
    detail = cbce_provider_rollout_status(
        "tieba", "cbce_tieba", "fetch_detail"
    )
    assert detail and detail["ready"] is True


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        '{"unknown":{"search":"provider"}}',
        '{"x":{"search":"x_embed"}}',
        '{"bilibili":{"scan_channel":"bilibili_open_platform"}}',
    ],
)
def test_provider_override_rejects_invalid_rollout_configuration(raw: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        provider_overrides(raw)


def test_cbce_browser_preflight_is_safe_and_does_not_expose_local_path(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_browser_executable_path", executable)
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles-v2"
    )
    monkeypatch.setattr(
        settings, "mediacrawler_profile_dir", tmp_path / "legacy-profile"
    )
    status = cbce_browser_preflight()
    assert status["executable_configured"] is True
    assert status["profile_namespace"] == "v2"
    assert str(tmp_path) not in str(status)


def test_cbce_browser_preflight_rejects_legacy_profile_overlap(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    legacy = tmp_path / "legacy-profile"
    monkeypatch.setattr(settings, "content_bot_cbce_browser_executable_path", executable)
    monkeypatch.setattr(settings, "content_bot_cbce_profile_root", legacy / "v2")
    monkeypatch.setattr(settings, "mediacrawler_profile_dir", legacy)
    status = cbce_browser_preflight()
    assert status["ready"] is False
    assert status["reason_code"] == "BROWSER_EXECUTABLE_OR_PROFILE_INVALID"


def test_cbce_browser_preflight_can_reuse_configured_coccoc_binary(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_browser_executable_path", None)
    monkeypatch.setattr(settings, "content_bot_coccoc_executable_path", executable)
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles-v2"
    )
    monkeypatch.setattr(
        settings, "mediacrawler_profile_dir", tmp_path / "legacy-profile"
    )
    assert cbce_browser_preflight()["executable_configured"] is True
