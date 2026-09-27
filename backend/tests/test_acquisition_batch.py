import sys
import time
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.services import acquisition as acquisition_module
from app.services.acquisition import AcquisitionError, AcquisitionManager
from app.services.connector_contracts import RawContentItem
from app.sqlite_store import SQLiteStore

TARGETS = ["https://www.youtube.com/@first", "https://www.youtube.com/@second"]


class NoDownloads:
    def list(self):
        return []


def entry(identity):
    return {"_id": identity, "source_id": "youtube", "external_id": identity,
            "media_id": identity, "title": identity, "media_type": "video",
            "canonical_url": f"https://www.youtube.com/watch?v={identity}"}


def service(tmp_path, extractor):
    store = SQLiteStore(tmp_path / "batch.db")
    store.initialize()
    return AcquisitionManager(store, NoDownloads(), max_workers=1, extractor=extractor)


def wait_done(manager, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = manager.get_run(run_id)
        if run["state"] not in {"running", "queued"} and run_id not in manager._futures:
            return run
        time.sleep(0.01)
    raise AssertionError("Batch did not finish")


def test_batch_keeps_success_and_retries_only_failed_target(tmp_path):
    visits = []
    fail = True

    def extract(request):
        target = request["targets"][0]
        visits.append(target)
        if target == TARGETS[1] and fail:
            raise AcquisitionError("Source offline", code="SOURCE_UNAVAILABLE")
        return [entry("video-first" if target == TARGETS[0] else "video-second")]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": TARGETS})
        result = wait_done(manager, run["id"])
        assert result["state"] == "partial"
        assert [child["state"] for child in result["children"]] == ["completed", "failed"]
        assert manager.list_candidates(run["id"])["total"] == 1
        assert len(manager.list_runs()) == 1
        first_id = result["children"][0]["id"]
        fail = False
        manager.resume(run["id"])
        result = wait_done(manager, run["id"])
        assert result["state"] == "completed"
        assert result["children"][0]["id"] == first_id
        assert visits == [TARGETS[0], TARGETS[1], TARGETS[1]]
        assert manager.list_candidates(run["id"])["total"] == 2
        assert result["counters"]["new"] == 2
    finally:
        manager.shutdown()


def test_batch_unsupported_source_does_not_hide_other_channel(tmp_path):
    visits = []

    def extract(request):
        visits.extend(request["targets"])
        return [entry("public-video")]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": [
            "https://space.bilibili.com/2", TARGETS[0],
        ]})
        result = wait_done(manager, run["id"])
        assert result["state"] == "partial"
        assert result["children"][0]["error_code"] == "UNSUPPORTED_OPERATION"
        assert visits == [TARGETS[0]]
        assert manager.list_candidates(run["id"])["total"] == 1
    finally:
        manager.shutdown()


def test_batch_unions_duplicate_candidates_and_shares_metadata_budget(tmp_path):
    budgets = []

    def extract(request):
        budgets.append(request["limits"]["max_candidates"])
        return [entry("same-video")]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": [*TARGETS, "https://www.youtube.com/@third"],
                                  "limits": {"max_candidates": 2}})
        result = wait_done(manager, run["id"])
        assert budgets == [1, 1]
        assert result["state"] == "partial"
        assert result["children"][2]["state"] == "skipped"
        assert result["children"][2]["stop_reason"] == "batch_budget"
        assert result["counters"]["scanned"] == 2
        assert result["counters"]["new"] == 1
        assert result["counters"]["duplicate"] == 1
        assert manager.list_candidates(run["id"])["total"] == 1
    finally:
        manager.shutdown()


@pytest.mark.parametrize("action, stopped", [("pause", "paused"), ("cancel", "canceled")])
def test_batch_control_stops_current_and_unstarted_children(tmp_path, action, stopped):
    entered, release = Event(), Event()
    visits = []

    def extract(request):
        visits.extend(request["targets"])
        entered.set()
        assert release.wait(5)
        return [entry("delayed-video")]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": TARGETS})
        assert entered.wait(5)
        child_id = manager.get_run(run["id"])["children"][0]["id"]
        with pytest.raises(AcquisitionError, match="nhóm"):
            getattr(manager, action)(child_id)
        getattr(manager, action)(run["id"])
        release.set()
        result = wait_done(manager, run["id"])
        assert result["state"] == stopped
        assert all(child["state"] == stopped for child in result["children"])
        assert visits == [TARGETS[0]]
        assert manager.list_candidates(run["id"])["total"] == 0
        manager.resume(run["id"])
        assert wait_done(manager, run["id"])["state"] == "completed"
    finally:
        release.set()
        manager.shutdown()


def test_batch_restart_reuses_completed_child(tmp_path):
    visits = []
    manager = service(tmp_path, lambda request: visits.extend(request["targets"]) or [entry(request["targets"][0].split("@")[-1])])
    try:
        run = manager.create_run({"mode": "creator", "targets": TARGETS})
        result = wait_done(manager, run["id"])
        # Simulate a crash after both child publications, before parent commit.
        manager.store.upsert_acquisition_document("acquisition_runs", {**result, "state": "running"})
    finally:
        manager.shutdown()
    recovered = service(tmp_path, lambda request: pytest.fail("Completed child must not be crawled again"))
    try:
        recovered.start()
        assert recovered.get_run(run["id"])["state"] == "paused"
        recovered.resume(run["id"])
        result = wait_done(recovered, run["id"])
        assert result["state"] == "completed"
        assert recovered.list_candidates(run["id"])["total"] == 2
        assert visits == TARGETS
    finally:
        recovered.shutdown()


def test_batch_routes_mixed_creators_to_their_own_provider(tmp_path, monkeypatch):
    from app.services import acquisition_process

    visited = []

    class BilibiliConnector:
        configured = True

        async def list_creator(self, target, *, max_items):
            visited.append(("cbce_bilibili", target))
            yield RawContentItem(external_id="BV1xx411c7mD", title="Bilibili fixture",
                                 canonical_url="https://www.bilibili.com/video/BV1xx411c7mD")

    def youtube_metadata(request, cancellation):
        assert not cancellation.is_set()
        assert request["targets"] == [TARGETS[0]]
        visited.append(("yt-dlp", TARGETS[0]))
        return [entry("yt-fixture")]

    monkeypatch.setattr(acquisition_process, "extract_metadata", youtube_metadata)
    store = SQLiteStore(tmp_path / "mixed.db")
    store.initialize()
    manager = AcquisitionManager(store, NoDownloads(), max_workers=1,
                                 connectors={"bilibili": BilibiliConnector()})
    try:
        run = manager.create_run({"mode": "creator", "targets": [TARGETS[0], "https://space.bilibili.com/2"]})
        result = wait_done(manager, run["id"])
        assert result["state"] == "completed"
        assert [child["provider_id"] for child in result["children"]] == ["yt-dlp", "cbce_bilibili"]
        assert visited == [("yt-dlp", TARGETS[0]), ("cbce_bilibili", "https://space.bilibili.com/2")]
        assert {item["source_id"] for item in manager.list_candidates(run["id"])["items"]} == {"youtube", "bilibili"}
    finally:
        manager.shutdown()


def test_batch_deadline_is_shared_by_all_targets(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(acquisition_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    visits = []

    def extract(request):
        visits.extend(request["targets"])
        now[0] += 31
        return [entry("within-first-target")]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": TARGETS,
                                  "limits": {"deadline_seconds": 30}})
        result = wait_done(manager, run["id"])
        assert result["state"] == "partial"
        assert visits == [TARGETS[0]]
        assert result["children"][1]["stop_reason"] == "batch_budget"
    finally:
        manager.shutdown()


@pytest.mark.parametrize("provider_honors_limit", [True, False])
def test_batch_fair_share_prevents_first_channel_using_entire_budget(tmp_path, provider_honors_limit):
    budgets = []

    def extract(request):
        prefix = request["targets"][0].split("@")[-1]
        budgets.append(request["limits"]["max_candidates"])
        count = request["limits"]["max_candidates"] if provider_honors_limit else 100
        return [entry(f"{prefix}-{index}") for index in range(count)]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": TARGETS, "limits": {"max_candidates": 20}})
        result = wait_done(manager, run["id"])
        assert result["state"] == "completed"
        assert result["stop_reason"] == "candidate_budget"
        assert budgets == [10, 10]
        assert [child["counters"]["new"] for child in result["children"]] == [10, 10]
        assert manager.list_candidates(run["id"])["total"] == 20
    finally:
        manager.shutdown()


def test_batch_http_reports_partial_results_and_rejects_child_control(tmp_path, application_services):
    def extract(request):
        if request["targets"] == [TARGETS[1]]:
            raise AcquisitionError("Login needed", code="AUTH_REQUIRED")
        return [entry("http-fixture")]

    manager = service(tmp_path, extract)
    application_services.acquisition = manager
    try:
        with TestClient(main.app) as client:
            response = client.post("/api/v1/acquisition/runs", json={"mode": "creator", "targets": TARGETS})
            assert response.status_code == 202
            result = wait_done(manager, response.json()["id"])
            details = client.get(f"/api/v1/acquisition/runs/{result['id']}").json()
            assert details["state"] == "partial"
            assert details["children"][1]["error_code"] == "AUTH_REQUIRED"
            child_id = details["children"][0]["id"]
            for action in ("pause", "cancel", "resume"):
                response = client.post(f"/api/v1/acquisition/runs/{child_id}/{action}")
                assert response.status_code == 409
                assert response.json()["detail"]["run_id"] == result["id"]
            assert client.get(f"/api/v1/acquisition/runs/{result['id']}/candidates").json()["total"] == 1
    finally:
        manager.shutdown()


def test_youtube_creator_continue_keeps_old_candidates_and_advances_generation(tmp_path):
    calls = []

    def extract(request):
        offset = int(request.get("listing_offset", 0))
        calls.append((offset, request["limits"]["max_candidates"]))
        return [entry(f"video-{index}") for index in range(offset, offset + 2)]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": [TARGETS[0]],
                                  "limits": {"max_candidates": 2, "max_pages": 2}})
        result = wait_done(manager, run["id"])
        assert result["state"] == "completed"
        assert result["can_continue"] is True
        assert result["pagination"]["generation"] == 0
        assert manager.list_candidates(run["id"])["total"] == 2

        continued = manager.continue_run(run["id"], expected_generation=0)
        assert continued["state"] == "queued"
        result = wait_done(manager, run["id"])
        assert result["pagination"]["generation"] == 1
        assert result["can_continue"] is True
        assert manager.list_candidates(run["id"])["total"] == 4
        assert calls == [(0, 2), (2, 2)]

        replay = manager.continue_run(run["id"], expected_generation=0)
        assert replay["pagination"]["generation"] == 1
        assert replay["state"] == "completed"
    finally:
        manager.shutdown()


def test_youtube_creator_cursor_repetition_stops_continuation(tmp_path):
    def extract(_request):
        return [entry("same-video-1"), entry("same-video-2")]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run({"mode": "creator", "targets": [TARGETS[0]],
                                  "limits": {"max_candidates": 2, "max_pages": 3}})
        result = wait_done(manager, run["id"])
        assert result["can_continue"] is True
        manager.continue_run(run["id"], expected_generation=0)
        result = wait_done(manager, run["id"])
        assert result["state"] == "completed"
        assert result["pagination"]["last_stop_reason"] == "no_new_items"
        assert result["pagination"]["exhausted"] is True
        assert result["can_continue"] is False
        assert manager.list_candidates(run["id"])["total"] == 2
    finally:
        manager.shutdown()


def test_ytdlp_continuation_uses_playlist_position_window(monkeypatch):
    captured = []

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, target, download=False):
            assert download is False
            assert target == "https://www.youtube.com/@fixture"
            return {"entries": [{"id": "video-1", "title": "Fixture", "playlist_index": 4,
                                  "webpage_url": "https://www.youtube.com/watch?v=video-1"}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    rows = AcquisitionManager._extract_with_yt_dlp({
        "mode": "creator", "targets": ["https://www.youtube.com/@fixture"],
        "listing_offset": 3,
        "limits": {"max_candidates": 2, "max_pages": 2, "deadline_seconds": 30},
    })
    assert rows[0]["listing_position"] == 4
    assert captured[0]["playliststart"] == 4
    assert captured[0]["playlistend"] == 5


def test_bilibili_playlist_continuation_advances_position_window(tmp_path):
    calls = []

    def extract(request):
        offset = int(request.get("listing_offset", 0))
        calls.append(offset)
        return [
            {
                "_id": f"bili-{offset + index}",
                "source_id": "bilibili",
                "provider_id": "yt-dlp",
                "external_id": f"BV{offset + index:010d}",
                "media_id": f"BV{offset + index:010d}",
                "canonical_url": (
                    f"https://www.bilibili.com/video/BV{offset + index:010d}"
                ),
                "title": f"Bilibili {offset + index}",
                "media_type": "video",
                "availability": "metadata",
                "listing_position": offset + index + 1,
            }
            for index in range(2)
        ]

    manager = service(tmp_path, extract)
    try:
        run = manager.create_run(
            {
                "mode": "playlist",
                "targets": ["https://www.bilibili.com/medialist/detail/ml123"],
                "limits": {"max_candidates": 2, "max_pages": 2, "deadline_seconds": 30},
            }
        )
        manager._futures[run["id"]].result(timeout=3)
        initial = manager.get_run(run["id"])
        assert initial["can_continue"] is True
        assert initial["pagination"]["kind"] == "bilibili_position_v1"

        manager.continue_run(
            run["id"], expected_generation=initial["pagination"]["generation"]
        )
        manager._futures[run["id"]].result(timeout=3)
        continued = manager.get_run(run["id"])
        assert calls == [0, 2]
        assert manager.list_candidates(run["id"])["total"] == 4
        assert continued["pagination"]["next_offset"] == 4
    finally:
        manager.shutdown()


def test_continuation_route_advances_once_and_replays_old_generation(
    tmp_path, application_services
):
    calls = []

    def extract(request):
        offset = int(request.get("listing_offset", 0))
        calls.append(offset)
        return [entry(f"api-video-{index}") for index in range(offset, offset + 2)]

    manager = service(tmp_path, extract)
    application_services.acquisition = manager
    try:
        with TestClient(main.app) as client:
            response = client.post("/api/v1/acquisition/runs", json={
                "mode": "creator", "targets": [TARGETS[0]],
                "limits": {"max_candidates": 2, "max_pages": 2, "deadline_seconds": 30},
            })
            assert response.status_code == 202
            run_id = response.json()["id"]
            result = wait_done(manager, run_id)
            assert result["can_continue"] is True
            response = client.post(f"/api/v1/acquisition/runs/{run_id}/continue",
                                   json={"expected_generation": 0})
            assert response.status_code == 202
            result = wait_done(manager, run_id)
            assert result["pagination"]["generation"] == 1
            assert calls == [0, 2]
            response = client.post(f"/api/v1/acquisition/runs/{run_id}/continue",
                                   json={"expected_generation": 0})
            assert response.status_code == 202
            assert response.json()["pagination"]["generation"] == 1
            assert calls == [0, 2]
    finally:
        manager.shutdown()
