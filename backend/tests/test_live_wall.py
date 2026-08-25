from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.crawlers.runtime import PlaywrightPersistentDriver
from app.services.live_wall import (
    DisplayBounds,
    LiveWallChannel,
    LiveWallFailure,
    LiveWallManager,
    _browser_failure,
    tile_bounds,
)


def _topic_payload(name: str, channels: list[dict]) -> dict:
    return {
        "name": name,
        "include_terms": [],
        "exclude_terms": [],
        "source_ids": [],
        "channels": channels,
        "enabled": False,
        "interval_minutes": 360,
        "max_items_per_source": 10,
    }


def test_live_wall_config_defaults_validates_and_prunes_removed_channels() -> None:
    with TestClient(main.app) as client:
        created_response = client.post(
            "/api/v1/keywords",
            json=_topic_payload(
                "Live Wall API",
                [
                    {"url": "https://x.com/NTE_Ani_Info"},
                    {"url": "https://www.tiktok.com/@game.dev"},
                    {"url": "https://www.reddit.com/r/gaming/"},
                ],
            ),
        )
        assert created_response.status_code == 201
        created = created_response.json()
        assert created["live_wall"] == {"channel_ids": [], "slots": 4}
        ids = [channel["id"] for channel in created["channels"]]

        saved = client.put(
            f"/api/v1/keywords/{created['id']}/live-wall",
            json={"channel_ids": [ids[2], ids[0], ids[1]], "slots": 4},
        )
        assert saved.status_code == 200
        assert saved.json()["live_wall"]["channel_ids"] == [ids[2], ids[0], ids[1]]

        duplicate = client.put(
            f"/api/v1/keywords/{created['id']}/live-wall",
            json={"channel_ids": [ids[0], ids[0]], "slots": 4},
        )
        assert duplicate.status_code == 422
        invalid_slot = client.put(
            f"/api/v1/keywords/{created['id']}/live-wall",
            json={"channel_ids": [], "slots": 3},
        )
        assert invalid_slot.status_code == 422
        unknown = client.put(
            f"/api/v1/keywords/{created['id']}/live-wall",
            json={"channel_ids": ["not-in-topic"], "slots": 4},
        )
        assert unknown.status_code == 422

        updated = client.patch(
            f"/api/v1/keywords/{created['id']}",
            json=_topic_payload(
                created["name"],
                [created["channels"][0], created["channels"][2]],
            ),
        )
        assert updated.status_code == 200
        assert updated.json()["live_wall"] == {
            "channel_ids": [ids[2], ids[0]],
            "slots": 4,
        }


def test_legacy_topic_without_live_wall_gets_a_compatible_default(mongo_store) -> None:
    now = datetime.now(UTC)
    created = mongo_store.create_keyword(
        {
            "name": "Legacy Live Wall topic",
            "normalized_name": "legacy live wall topic",
            "source_ids": [],
            "channels": [],
            "enabled": False,
            "created_at": now,
            "updated_at": now,
        }
    )
    with TestClient(main.app) as client:
        topics = client.get("/api/v1/keywords").json()
    topic = next(row for row in topics if row["id"] == created["id"])
    assert topic["live_wall"] == {"channel_ids": [], "slots": 4}


def test_live_wall_session_api_rejects_urls_and_non_watchlist_ids() -> None:
    with TestClient(main.app) as client:
        created = client.post(
            "/api/v1/keywords",
            json=_topic_payload(
                "Live Wall allowlist",
                [{"url": "https://x.com/NTE_Ani_Info"}],
            ),
        ).json()
        display = {"left": 0, "top": 0, "width": 1920, "height": 1040}
        direct_url = client.put(
            "/api/v1/live-wall/session",
            json={
                "keyword_id": created["id"],
                "channel_ids": [created["channels"][0]["id"]],
                "display": display,
                "url": "https://example.test/not-allowed",
            },
        )
        assert direct_url.status_code == 422
        unknown = client.put(
            "/api/v1/live-wall/session",
            json={
                "keyword_id": created["id"],
                "channel_ids": ["unknown-channel"],
                "display": display,
            },
        )
        assert unknown.status_code == 422


@pytest.mark.parametrize(
    ("count", "columns", "rows"),
    [(1, 1, 1), (2, 2, 1), (3, 2, 2), (4, 2, 2), (5, 3, 2), (6, 3, 2)],
)
def test_live_wall_geometry_stays_inside_display(
    count: int, columns: int, rows: int
) -> None:
    display = DisplayBounds(left=-1920, top=20, width=1920, height=1000)
    bounds = tile_bounds(display, count)
    assert len(bounds) == count
    assert len({entry["left"] for entry in bounds}) == columns
    assert len({entry["top"] for entry in bounds}) == rows
    assert all(entry["left"] >= display.left for entry in bounds)
    assert all(entry["top"] >= display.top for entry in bounds)
    assert all(entry["left"] + entry["width"] <= display.left + display.width for entry in bounds)
    assert all(entry["top"] + entry["height"] <= display.top + display.height for entry in bounds)


class FakeCdp:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.calls: list[tuple[str, dict | None]] = []
        self.sequence = 0

    async def send(self, method: str, params: dict | None = None):
        self.calls.append((method, params))
        if method == "Target.createTarget":
            self.sequence += 1
            target_id = f"target-{self.sequence}"
            self.active.add(target_id)
            return {"targetId": target_id}
        if method == "Browser.getWindowForTarget":
            return {"windowId": self.sequence or 1, "bounds": {}}
        if method == "Browser.setWindowBounds":
            return {}
        if method == "Target.closeTarget":
            self.active.discard(str((params or {}).get("targetId") or ""))
            return {"success": True}
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {"targetId": target_id, "type": "page"}
                    for target_id in sorted(self.active)
                ]
            }
        raise AssertionError(f"Unexpected CDP method: {method}")


class FakeBrowser:
    def __init__(self, cdp: FakeCdp) -> None:
        self.cdp = cdp

    async def new_browser_cdp_session(self) -> FakeCdp:
        return self.cdp


class FakeContext:
    def __init__(self, cdp: FakeCdp) -> None:
        self.browser = FakeBrowser(cdp)
        self.pages: list = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self.context = context

    async def launch_persistent_context(self, **_kwargs):
        return self.context


class FakeRuntime:
    def __init__(self) -> None:
        self.cdp = FakeCdp()
        self.context = FakeContext(self.cdp)
        self.chromium = FakeChromium(self.context)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _channels(count: int, *, offset: int = 0) -> list[LiveWallChannel]:
    return [
        LiveWallChannel(
            id=f"channel-{index + offset}",
            source_id="x",
            label=f"Channel {index + offset}",
            url=f"https://x.com/channel_{index + offset}",
        )
        for index in range(count)
    ]


def test_live_wall_manager_reflows_tracks_closure_and_is_singleton(tmp_path: Path) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    runtimes: list[FakeRuntime] = []

    def driver_factory() -> PlaywrightPersistentDriver:
        runtime = FakeRuntime()
        runtimes.append(runtime)

        async def runtime_factory():
            return runtime

        return PlaywrightPersistentDriver(runtime_factory)

    manager = LiveWallManager(
        profile_root=tmp_path / "live-wall-profiles",
        browser_executable=executable,
        driver_factory=driver_factory,
    )
    display = DisplayBounds(0, 0, 1920, 1040)

    async def run() -> None:
        opened = await manager.open_or_update("user-a", 1, _channels(4), display)
        assert opened["state"] == "ready"
        assert len(opened["windows"]) == 4
        assert "url" not in str(opened).lower()
        assert "profile" not in str(opened).lower()
        with pytest.raises(LiveWallFailure) as error:
            await manager.open_or_update("user-b", 1, _channels(1), display)
        assert error.value.code == "LIVE_WALL_IN_USE"
        with pytest.raises(LiveWallFailure) as error:
            await manager.close("user-b")
        assert error.value.code == "LIVE_WALL_IN_USE"
        with pytest.raises(LiveWallFailure) as error:
            await manager.delete_profile("user-a", "DELETE LIVE WALL PROFILE")
        assert error.value.code == "LIVE_WALL_PROFILE_IN_USE"

        first_target = manager._windows[0].target_id
        assert first_target is not None
        runtimes[0].cdp.active.remove(first_target)
        status = await manager.status("user-a")
        assert status["windows"][0]["state"] == "closed"
        assert status["windows"][0]["reason_code"] == "WINDOW_CLOSED_BY_USER"

        updated = await manager.open_or_update("user-a", 2, _channels(2, offset=10), display)
        assert [window["channel_id"] for window in updated["windows"]] == [
            "channel-10",
            "channel-11",
        ]
        await manager.shutdown()
        assert (tmp_path / "live-wall-profiles").exists()

    asyncio.run(run())
    assert runtimes[0].context.closed is True
    assert runtimes[0].stopped is True
    set_bounds = [
        params
        for method, params in runtimes[0].cdp.calls
        if method == "Browser.setWindowBounds"
    ]
    assert len(set_bounds) == 6
    assert all("url" not in (params or {}) for params in set_bounds)
    assert manager._profile("user-a").path != manager._profile("user-b").path


def test_live_wall_manager_reports_missing_coccoc_before_driver_launch(
    tmp_path: Path,
) -> None:
    manager = LiveWallManager(
        profile_root=tmp_path / "live-wall-profiles",
        browser_executable=tmp_path / "missing-browser.exe",
    )

    async def run() -> None:
        with pytest.raises(LiveWallFailure) as error:
            await manager.open_or_update(
                "user-a",
                1,
                _channels(1),
                DisplayBounds(0, 0, 1920, 1040),
            )
        assert error.value.code == "BROWSER_EXECUTABLE_NOT_FOUND"

    asyncio.run(run())


def test_live_wall_browser_failure_classifies_windows_subprocess_loop() -> None:
    code, detail = _browser_failure(NotImplementedError())
    assert code == "BROWSER_SUBPROCESS_UNAVAILABLE"
    assert "Windows" in detail


def test_live_wall_browser_failure_does_not_expose_unknown_exception_text() -> None:
    secret = r"D:\private\live-wall-profile\cookie-secret"
    code, detail = _browser_failure(RuntimeError(secret))
    assert code == "LIVE_WALL_BROWSER_FAILED"
    assert secret not in detail
