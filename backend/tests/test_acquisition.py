from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.acquisition as acquisition_api
import app.services.acquisition as acquisition_module
from app import main
from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.acquisition import AcquisitionError, AcquisitionManager
from app.sqlite_store import SQLiteStore


class FakeDownloads:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.jobs: list[dict] = []

    def list(self) -> list[dict]:
        return [dict(job) for job in self.jobs]

    def queue_capacity(self) -> int:
        active = sum(job.get("state") in {"queued", "running"} for job in self.jobs)
        return max(0, 32 - active)

    def get(self, job_id):
        job = next((item for item in self.jobs if item["id"] == job_id), None)
        return dict(job) if job is not None else None

    def job_is_published(self, job_id):
        job = self.get(job_id)
        return bool(job and job.get("state") == "succeeded")

    def submit(
        self,
        urls,
        quality="1080",
        cookie_text=None,
        connection_id=None,
        intent_keys=None,
    ):
        del cookie_text
        self.calls.append(list(urls))
        rows = []
        for index, url in enumerate(urls):
            intent_key = intent_keys[index] if intent_keys is not None else None
            existing = next(
                (
                    job
                    for job in self.jobs
                    if job["url"] == url
                    and job["quality"] == quality
                    and job.get("connection_id") == connection_id
                ),
                None,
            )
            if existing is None:
                existing = {
                    "id": f"job-{len(self.jobs) + 1}",
                    "url": url,
                    "quality": quality,
                    "connection_id": connection_id,
                    "intent_key": intent_key,
                    "state": "queued",
                    "error": None,
                }
                self.jobs.append(existing)
            rows.append(dict(existing))
        return rows

    def pause(self, job_id):
        job = next(item for item in self.jobs if item["id"] == job_id)
        job["state"] = "paused"
        return dict(job)

    def cancel(self, job_id):
        job = next(item for item in self.jobs if item["id"] == job_id)
        job["state"] = "canceled"
        return dict(job)

    def retry(self, job_id, cookie_text=None, connection_id=None):
        del cookie_text
        job = next(item for item in self.jobs if item["id"] == job_id)
        if connection_id is not None:
            assert job.get("connection_id") == connection_id
        job["state"] = "queued"
        return dict(job)


def candidate(url: str = "https://www.youtube.com/watch?v=abc123", candidate_id: str = "candidate-1") -> dict:
    return {
        "_id": candidate_id,
        "source_id": "youtube",
        "provider_id": "yt-dlp",
        "external_id": "abc123",
        "media_id": "abc123",
        "canonical_url": url,
        "title": "A source video",
        "media_type": "video",
        "availability": "metadata",
        "updated_at": "2026-09-21T00:00:00+00:00",
    }


def manager(tmp_path: Path, extractor=None):
    store = SQLiteStore(tmp_path / "acquisition.db")
    store.initialize()
    downloads = FakeDownloads()
    service = AcquisitionManager(store, downloads, extractor=extractor)
    return store, downloads, service


def test_metadata_queue_capacity_is_atomic_and_scheduler_defers(tmp_path):
    store = SQLiteStore(tmp_path / "bounded.db")
    store.initialize()
    release = Event()
    barrier = Barrier(2)
    accepted, rejected = [], []

    def extract(_request):
        assert release.wait(5)
        return []

    service = AcquisitionManager(store, FakeDownloads(), max_pending=1, extractor=extract)

    def submit():
        barrier.wait(timeout=3)
        try:
            accepted.append(service.create_run({
                "mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"],
            }))
        except AcquisitionError as error:
            rejected.append(error.code)

    try:
        threads = [Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        assert len(accepted) == 1
        assert rejected == ["QUEUE_FULL"]
        assert len(service.list_runs()) == 1

        channel = service.save_channel("https://www.youtube.com/@creator")
        service.update_subscription(channel["id"], enabled=True)
        service.scheduler_tick()
        subscription = service.list_channels()[0]["subscription"]
        assert subscription["last_status"] == "waiting_capacity"
        assert subscription["active_run_id"] is None
        assert subscription["lease_expires_at"] is None
        assert len(service.list_runs()) == 1
    finally:
        release.set()
        service.shutdown()


@pytest.mark.parametrize(
    ("retry_after_seconds", "expected"),
    [(17.2, 18), (None, 60), (-4, 0), (10**12, 7 * 24 * 60 * 60)],
)
def test_provider_rate_limit_retry_metadata_is_safe_and_actionable(
    retry_after_seconds, expected
):
    failure = CrawlerFailure(
        CrawlerErrorCode.RATE_LIMITED,
        "Provider temporarily limited the session.",
        retryable=True,
        retry_after_seconds=retry_after_seconds,
    )

    mapped = AcquisitionManager._map_crawler_failure(failure)

    assert mapped.code == "RATE_LIMITED"
    assert mapped.retry_after == expected


def test_rate_limited_metadata_retries_and_reuses_one_deadline(monkeypatch, tmp_path):
    attempts = []

    def extract(request):
        attempts.append(request["limits"]["deadline_seconds"])
        if len(attempts) < 3:
            raise AcquisitionError("Try again later.", code="RATE_LIMITED", retry_after=0)
        return [candidate()]

    _store, _downloads, service = manager(tmp_path, extractor=extract)
    monkeypatch.setattr(acquisition_module.random, "uniform", lambda _start, _stop: 0.0)
    try:
        entries = service._extract_with_retry(
            {"limits": {"deadline_seconds": 5}}, Event()
        )
    finally:
        service.shutdown()

    assert [entry["_id"] for entry in entries] == ["candidate-1"]
    assert len(attempts) == 3
    assert attempts[0] > attempts[1] > attempts[2] > 0


def test_rate_limited_metadata_stops_after_three_total_attempts(monkeypatch, tmp_path):
    attempts = 0

    def extract(_request):
        nonlocal attempts
        attempts += 1
        raise AcquisitionError("Try again later.", code="RATE_LIMITED", retry_after=0)

    _store, _downloads, service = manager(tmp_path, extractor=extract)
    monkeypatch.setattr(acquisition_module.random, "uniform", lambda _start, _stop: 0.0)
    try:
        with pytest.raises(AcquisitionError, match="Try again later"):
            service._extract_with_retry({"limits": {"deadline_seconds": 5}}, Event())
    finally:
        service.shutdown()

    assert attempts == 3


def test_provider_retry_metadata_is_persisted_on_failed_run(tmp_path):
    def extract(_request):
        raise AcquisitionError(
            "Provider temporarily limited the session.",
            code="RATE_LIMITED",
            retry_after=0,
        )

    _store, _downloads, service = manager(tmp_path, extractor=extract)
    try:
        run = service.create_run(
            {"mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"]}
        )
        deadline = time.monotonic() + 3
        current = service.get_run(run["id"])
        while current and current["state"] not in {"failed", "completed"} and time.monotonic() < deadline:
            time.sleep(0.01)
            current = service.get_run(run["id"])

        assert current is not None
        assert current["state"] == "failed"
        assert current["error_code"] == "RATE_LIMITED"
        assert current["retry_after"] == 0
    finally:
        service.shutdown()


def test_provider_retry_metadata_is_exposed_as_http_retry_after_header():
    with pytest.raises(HTTPException) as caught:
        acquisition_api._raise_acquisition(
            AcquisitionError("Try again later.", code="RATE_LIMITED", retry_after=60)
        )

    assert caught.value.status_code == 429
    assert caught.value.headers == {"Retry-After": "60"}
    assert caught.value.detail["retry_after"] == 60


def test_subscription_failure_uses_provider_retry_after(tmp_path):
    _store, _downloads, service = manager(tmp_path)
    try:
        channel = service.save_channel("https://www.youtube.com/@creator")
        before = datetime.now(UTC)
        service._fail_subscription(channel["id"], "Provider temporarily limited.", retry_after=60)
        subscription = service.list_channels()[0]["subscription"]
        next_run = datetime.fromisoformat(subscription["next_run_at"])

        assert subscription["last_status"] == "failed"
        assert 45 <= (next_run - before).total_seconds() <= 90
    finally:
        service.shutdown()


def test_queue_full_error_exposes_retry_after(tmp_path):
    store = SQLiteStore(tmp_path / "queue-error.db")
    store.initialize()
    entered = Event()
    release = Event()

    def extract(_request):
        entered.set()
        release.wait(5)
        return []

    service = AcquisitionManager(store, FakeDownloads(), max_pending=1, extractor=extract)
    try:
        service.create_run({"mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"]})
        assert entered.wait(3)
        with pytest.raises(AcquisitionError) as caught:
            service.create_run({"mode": "video", "targets": ["https://www.youtube.com/watch?v=def456"]})
        assert caught.value.code == "QUEUE_FULL"
        assert caught.value.retry_after == 60
        assert caught.value.run_id is None
        assert caught.value.job_id is None
    finally:
        release.set()
        service.shutdown()


@pytest.mark.parametrize("action", ["pause", "cancel"])
@pytest.mark.parametrize("failure", [None, AcquisitionError("late error"), RuntimeError("late error")])
def test_stopped_run_rejects_overlapping_resume_and_preserves_state(tmp_path, action, failure):
    entered, release = Event(), Event()

    def extract(_request):
        entered.set()
        assert release.wait(5)
        if failure:
            raise failure
        return [candidate()]

    _, _, service = manager(tmp_path, extractor=extract)
    try:
        run = service.create_run({
            "mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"],
        })
        assert entered.wait(3)
        future = service._futures[run["id"]]
        getattr(service, action)(run["id"])
        with pytest.raises(AcquisitionError) as caught:
            service.resume(run["id"])
        assert caught.value.code == "RUN_STOPPING"
        release.set()
        future.result(timeout=3)
        assert service.get_run(run["id"])["state"] == ("paused" if action == "pause" else "canceled")
        assert service.list_candidates(run["id"])["total"] == 0
    finally:
        release.set()
        service.shutdown()


def test_acquisition_run_does_not_require_keyword_and_persists_candidates(tmp_path):
    store, downloads, service = manager(
        tmp_path,
        extractor=lambda request: [candidate(request["targets"][0])],
    )
    run = service.create_run(
        {
            "mode": "video",
            "targets": ["https://www.youtube.com/watch?v=abc123"],
            "limits": {"max_candidates": 20},
        }
    )
    service._futures[run["id"]].result(timeout=3)

    saved = service.get_run(run["id"])
    assert saved["state"] == "completed"
    page = service.list_candidates(run["id"])
    assert page["total"] == 1
    assert page["items"][0]["title"] == "A source video"
    assert downloads.calls == []

    reopened = SQLiteStore(store.path)
    reopened.initialize()
    assert reopened.acquisition_document("acquisition_runs", run["id"])["state"] == "completed"
    assert reopened.acquisition_document("acquisition_candidates", "candidate-1") is not None
    service.shutdown()


def test_acquisition_run_applies_metadata_filters_and_counts_filtered_items(tmp_path):
    store, _, service = manager(tmp_path)
    kept = candidate(candidate_id="kept")
    kept["title"] = "Keep this camera review"
    kept["duration_seconds"] = 90
    dropped = candidate(
        "https://www.youtube.com/watch?v=dropped",
        "dropped",
    )
    dropped["title"] = "Skip this short"
    dropped["duration_seconds"] = 12
    service._extractor = lambda _request: [kept, dropped]
    run = service.create_run(
        {
            "mode": "video",
            "targets": ["https://www.youtube.com/watch?v=abc123"],
            "filters": {
                "media_type": "video",
                "title_contains": "camera",
                "min_duration_seconds": 60,
            },
        }
    )
    service._futures[run["id"]].result(timeout=3)

    detail = service.get_run(run["id"])
    page = service.list_candidates(run["id"])
    assert detail["counters"] == {
        "scanned": 2,
        "new": 1,
        "duplicate": 0,
        "filtered": 1,
        "unavailable": 0,
    }
    assert page["total"] == 1
    assert page["items"][0]["id"] == "kept"
    assert store.acquisition_document("acquisition_candidates", "dropped") is not None
    service.shutdown()


def test_acquisition_rejects_unknown_filter(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        service.create_run(
            {
                "mode": "video",
                "targets": ["https://www.youtube.com/watch?v=abc123"],
                "filters": {"ranking": "popular"},
            }
        )
    except AcquisitionError as error:
        assert error.code == "UNSUPPORTED_OPERATION"
    else:
        raise AssertionError("unknown filter must not be silently ignored")
    service.shutdown()


def test_download_selection_is_idempotent_and_does_not_download_non_video(tmp_path):
    _, downloads, service = manager(
        tmp_path,
        extractor=lambda request: [candidate(request["targets"][0])],
    )
    run = service.create_run(
        {"mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"]}
    )
    service._futures[run["id"]].result(timeout=3)

    first = service.select_downloads(
        ["candidate-1"], quality="720", idempotency_key="selection-1"
    )
    second = service.select_downloads(
        ["candidate-1"], quality="720", idempotency_key="selection-1"
    )
    third = service.select_downloads(
        ["candidate-1"], quality="720", idempotency_key="selection-2"
    )
    assert first["id"] == second["id"]
    assert third["id"] != first["id"]
    assert first["total"] == 1
    assert first["counts"] == {"queued": 1}
    assert service.get_selection(first["id"])["total"] == 1
    assert service.get_selection(third["id"])["total"] == 1
    history = service.list_selections(limit=1)
    assert len(history) == 1
    assert history[0]["id"] == third["id"]
    assert history[0]["counts"] == {"queued": 1}
    assert downloads.calls == [["https://www.youtube.com/watch?v=abc123"]]
    service.shutdown()


def test_incomplete_selection_resumes_after_reservation_crash(tmp_path):
    store, downloads, service = manager(
        tmp_path,
        extractor=lambda request: [candidate(request["targets"][0])],
    )
    try:
        run = service.create_run(
            {"mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"]}
        )
        service._futures[run["id"]].result(timeout=3)
        store.upsert_acquisition_document(
            "acquisition_selections",
            {
                "_id": "selection-before-crash",
                "idempotency_key": "crashed-selection",
                "quality": "720",
                "candidate_ids": ["candidate-1"],
                "state": "queued",
            },
        )

        first = service.select_downloads(
            ["candidate-1"], quality="720", idempotency_key="crashed-selection"
        )
        second = service.select_downloads(
            ["candidate-1"], quality="720", idempotency_key="crashed-selection"
        )
        assert first["id"] == second["id"] == "selection-before-crash"
        assert first["total"] == second["total"] == 1
        assert len(downloads.jobs) == 1
    finally:
        service.shutdown()


def test_pending_intent_reuses_job_when_db_link_was_lost(tmp_path):
    store, downloads, service = manager(tmp_path)
    try:
        store.upsert_acquisition_document("acquisition_candidates", candidate())
        store.upsert_acquisition_document(
            "acquisition_download_intents",
            {
                "_id": "candidate-1:720",
                "candidate_id": "candidate-1",
                "quality": "720",
                "state": "pending",
                "job_id": None,
                "requires_cookies": False,
            },
        )
        service._drain_pending_downloads()
        first = store.acquisition_document(
            "acquisition_download_intents", "candidate-1:720"
        )
        assert first["job_id"] == "job-1"
        assert downloads.jobs[0]["intent_key"] == "candidate-1:720"

        first.update(state="pending", job_id=None)
        store.upsert_acquisition_document("acquisition_download_intents", first)
        service._drain_pending_downloads()
        second = store.acquisition_document(
            "acquisition_download_intents", "candidate-1:720"
        )
        assert second["job_id"] == "job-1"
        assert len(downloads.jobs) == 1
    finally:
        service.shutdown()


def test_large_download_selection_keeps_items_pending_beyond_queue_capacity(tmp_path):
    _, downloads, service = manager(tmp_path)
    rows = [
        candidate(
            f"https://www.youtube.com/watch?v=id{index:03d}",
            f"candidate-{index}",
        )
        for index in range(50)
    ]
    service._extractor = lambda _request: rows
    run = service.create_run(
        {
            "mode": "playlist",
            "targets": ["https://www.youtube.com/playlist?list=PLlarge"],
            "limits": {"max_candidates": 100},
        }
    )
    service._futures[run["id"]].result(timeout=3)

    selection = service.select_downloads(
        [row["_id"] for row in rows],
        quality="720",
        idempotency_key="large-selection",
    )

    assert selection["total"] == 50
    assert selection["counts"] == {"queued": 32, "pending": 18}
    assert len(downloads.jobs) == 32
    assert [len(batch) for batch in downloads.calls] == [20, 12]

    for job in downloads.jobs[:2]:
        job["state"] = "succeeded"
    refreshed = service.get_selection(selection["id"])
    assert refreshed["counts"] == {"succeeded": 2, "queued": 32, "pending": 16}
    assert len(downloads.jobs) == 34
    assert len(downloads.calls[-1]) == 2
    service.shutdown()


def test_download_selection_group_controls_keep_intents_and_job_state(tmp_path):
    _, downloads, service = manager(
        tmp_path,
        extractor=lambda request: [candidate(request["targets"][0])],
    )
    run = service.create_run(
        {"mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"]}
    )
    service._futures[run["id"]].result(timeout=3)
    selection = service.select_downloads(
        ["candidate-1"], quality="720", idempotency_key="selection-control"
    )

    paused = service.control_selection(selection["id"], "pause")
    assert paused["state"] == "paused"
    assert paused["counts"] == {"paused": 1}

    resumed = service.control_selection(selection["id"], "resume")
    assert resumed["state"] == "queued"
    assert resumed["counts"] == {"queued": 1}

    canceled = service.control_selection(selection["id"], "cancel")
    assert canceled["state"] == "canceled"
    assert canceled["counts"] == {"canceled": 1}
    assert downloads.jobs[0]["state"] == "canceled"
    service.shutdown()


def test_search_is_explicitly_limited_to_youtube_in_first_provider_wave(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        service.create_run(
            {"mode": "search", "query": "cats", "source_id": "bilibili"}
        )
    except AcquisitionError as error:
        assert error.code == "UNSUPPORTED_OPERATION"
    else:
        raise AssertionError("non-YouTube search must not be accepted in P1")
    service.shutdown()


def test_acquisition_capability_matrix_separates_ready_and_unverified_operations(tmp_path):
    _, _, service = manager(tmp_path)
    matrix = service.capabilities()
    items = {item["source_id"]: item for item in matrix["items"]}

    assert items["youtube"]["operations"]["search"]["status"] == "ready"
    assert items["bilibili"]["operations"]["video"]["status"] == "ready"
    assert items["bilibili"]["operations"]["playlist"]["status"] == "ready"
    assert items["bilibili"]["operations"]["search"]["status"] == "setup_required"
    assert items["bilibili"]["operations"]["creator"]["status"] == "setup_required"
    assert items["douyin"]["operations"]["video"]["status"] == "unsupported"
    assert items["douyin"]["operations"]["search"]["status"] == "unsupported"
    youtube_video = items["youtube"]["operations"]["video"]
    assert youtube_video["implementation"] == "yt-dlp"
    assert youtube_video["target_kinds"] == ["content_url"]
    assert youtube_video["schedule_policy"] == "background_safe"
    assert youtube_video["coverage"] == "canary_verified"
    assert youtube_video["limits"]["max_candidates"] == 100
    service.shutdown()


def test_acquisition_feature_flag_blocks_new_work_but_allows_disablement(tmp_path, monkeypatch):
    store, _, service = manager(tmp_path)
    channel = service.save_channel("https://www.youtube.com/@creator")
    service.update_subscription(channel["id"], enabled=True)
    store.upsert_acquisition_document("acquisition_candidates", candidate())
    monkeypatch.setattr(acquisition_module.settings, "content_bot_acquisition_enabled", False)
    try:
        capabilities = service.capabilities()
        assert capabilities["feature_enabled"] is False
        assert capabilities["feature_disabled_reason"] == "FEATURE_DISABLED"

        with pytest.raises(AcquisitionError) as run_error:
            service.create_run(
                {"mode": "video", "targets": ["https://www.youtube.com/watch?v=abc123"]}
            )
        assert run_error.value.code == "FEATURE_DISABLED"

        with pytest.raises(AcquisitionError) as selection_error:
            service.select_downloads(["candidate-1"])
        assert selection_error.value.code == "FEATURE_DISABLED"

        service.scheduler_tick()
        assert service.list_runs() == []
        disabled = service.update_subscription(channel["id"], enabled=False)
        assert disabled is not None
        assert disabled["subscription"]["enabled"] is False
    finally:
        service.shutdown()


def test_configured_douyin_cbce_search_is_metadata_only_and_fail_closed(tmp_path):
    store = SQLiteStore(tmp_path / "douyin-search.db")
    store.initialize()
    downloads = FakeDownloads()

    class FakeDouyinConnector:
        configured = True
        spec = SimpleNamespace(provider_id="cbce_douyin")

        async def search(self, query):
            assert query.name == "game review"
            assert query.max_items == 2
            yield SimpleNamespace(
                external_id="7123456789012345678",
                canonical_url="https://www.douyin.com/video/7123456789012345678",
                title="Douyin game review",
                author="creator",
                published_at=None,
                metrics={"view_count": 12},
            )

    service = AcquisitionManager(
        store,
        downloads,
        max_workers=1,
        connectors={"douyin": FakeDouyinConnector()},
    )
    try:
        matrix = {
            item["source_id"]: item
            for item in service.capabilities()["items"]
        }
        assert matrix["douyin"]["operations"]["search"]["status"] == "unverified"
        assert matrix["douyin"]["operations"]["search"]["provider_id"] == "cbce_douyin"

        run = service.create_run(
            {
                "mode": "search",
                "source_id": "douyin",
                "provider_id": "cbce_douyin",
                "query": "game review",
                "limits": {"max_candidates": 2},
            }
        )
        service._futures[run["id"]].result(timeout=3)
        candidates = service.list_candidates(run["id"])["items"]
        assert service.get_run(run["id"])["provider_id"] == "cbce_douyin"
        assert candidates[0]["source_id"] == "douyin"
        assert candidates[0]["download_available"] is False

        selection = service.select_downloads([candidates[0]["id"]])
        assert downloads.calls == []
        intent = store.acquisition_document(
            "acquisition_download_intents", selection["intent_ids"][0]
        )
        assert intent["state"] == "failed"
        assert "media download contract" in intent["error"]
    finally:
        service.shutdown()


def test_douyin_search_does_not_fallback_to_ytdlp(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        with pytest.raises(AcquisitionError, match="Douyin") as error:
            service.create_run(
                {
                    "mode": "search",
                    "source_id": "douyin",
                    "query": "game review",
                }
            )
        assert error.value.code == "UNSUPPORTED_OPERATION"
    finally:
        service.shutdown()


def test_xhs_rednote_target_is_classified_before_unsupported_gate(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        with pytest.raises(AcquisitionError, match="xhs") as error:
            service.create_run(
                {
                    "mode": "video",
                    "targets": [
                        "https://www.rednote.com/explore/64abcdef0123456789abcdef"
                    ],
                }
            )
        assert error.value.code == "UNSUPPORTED_OPERATION"
    finally:
        service.shutdown()


def test_explicit_provider_id_cannot_spoof_or_bypass_operation_gate(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        service.create_run(
            {
                "mode": "video",
                "provider_id": "unknown-provider",
                "targets": ["https://www.youtube.com/watch?v=abc123"],
            }
        )
    except AcquisitionError as error:
        assert error.code == "UNSUPPORTED_OPERATION"
    else:
        raise AssertionError("unregistered provider must be rejected")
    service.shutdown()


def test_bilibili_creator_does_not_fallback_but_playlist_uses_ytdlp(tmp_path):
    calls = []

    def extract(request):
        calls.append(request)
        return [
            {
                "_id": "bili-playlist-candidate",
                "source_id": "bilibili",
                "provider_id": "yt-dlp",
                "external_id": "BV1oT4y1F7dX",
                "media_id": "BV1oT4y1F7dX",
                "canonical_url": "https://www.bilibili.com/video/BV1oT4y1F7dX",
                "title": "Playlist video",
                "media_type": "video",
                "availability": "metadata",
            }
        ]

    _, _, service = manager(tmp_path, extractor=extract)
    try:
        with pytest.raises(AcquisitionError, match="CBCE") as error:
            service.create_run(
                {
                    "mode": "creator",
                    "targets": ["https://space.bilibili.com/2"],
                }
            )
        assert error.value.code == "UNSUPPORTED_OPERATION"

        run = service.create_run(
            {
                "mode": "playlist",
                "targets": ["https://www.bilibili.com/medialist/detail/ml123"],
            }
        )
        service._futures[run["id"]].result(timeout=3)
        detail = service.get_run(run["id"])
        assert detail["state"] == "completed"
        assert detail["provider_id"] == "yt-dlp"
        assert calls[0]["mode"] == "playlist"
    finally:
        service.shutdown()


def test_connection_id_is_rejected_for_unwired_youtube_download(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        service.create_run(
            {
                "mode": "video",
                "connection_id": "browser-profile-1",
                "targets": ["https://www.youtube.com/watch?v=abc123"],
            }
        )
    except AcquisitionError as error:
        assert error.code == "UNSUPPORTED_OPERATION"
        assert "session bridge" in str(error)
    else:
        raise AssertionError("unwired connection must not be silently ignored")
    service.shutdown()


def test_bilibili_video_connection_id_is_preserved_for_download_bridge(tmp_path):
    _, _, service = manager(
        tmp_path,
        extractor=lambda request: [candidate(request["targets"][0])],
    )
    try:
        run = service.create_run(
            {
                "mode": "video",
                "connection_id": " Bili Main ",
                "targets": ["https://www.bilibili.com/video/BVbridge001"],
            }
        )
        service._futures[run["id"]].result(timeout=3)
        assert service.get_run(run["id"])["request"]["connection_id"] == "bili main"
    finally:
        service.shutdown()


def test_bilibili_async_preview_observes_cancel_event(tmp_path):
    store = SQLiteStore(tmp_path / "bilibili-cancel.db")
    store.initialize()
    downloads = FakeDownloads()
    entered = Event()

    class SlowBilibiliConnector:
        configured = True

        def for_account(self, _account_ref):
            return self

        async def fetch_detail(self, _target_url):
            entered.set()
            await asyncio.sleep(60)
            return SimpleNamespace(
                external_id="BVcancel001",
                title="Never reached",
                author="creator",
                published_at=None,
                metrics={},
            )

    service = AcquisitionManager(
        store,
        downloads,
        connectors={"bilibili": SlowBilibiliConnector()},
    )
    try:
        run = service.create_run(
            {
                "mode": "video",
                "connection_id": "profile one",
                "targets": ["https://www.bilibili.com/video/BVcancel001"],
            }
        )
        future = service._futures[run["id"]]
        assert entered.wait(3)
        canceled = service.cancel(run["id"])
        assert canceled["state"] == "canceled"
        future.result(timeout=3)
        assert service.get_run(run["id"])["state"] == "canceled"
    finally:
        service.shutdown()


def test_bilibili_connection_uses_cbce_detail_for_video_preview_when_ready(tmp_path):
    store = SQLiteStore(tmp_path / "bilibili-detail.db")
    store.initialize()
    downloads = FakeDownloads()
    visited = []

    class FakeBilibiliConnector:
        configured = True

        def for_account(self, account_ref):
            assert account_ref == "profile one"
            return self

        async def fetch_detail(self, target_url):
            visited.append(target_url)
            return SimpleNamespace(
                external_id="BVdetail001",
                title="Profile detail",
                author="creator",
                published_at=None,
                metrics={"view_count": 3},
            )

    service = AcquisitionManager(
        store,
        downloads,
        connectors={"bilibili": FakeBilibiliConnector()},
    )
    run = service.create_run(
        {
            "mode": "video",
            "connection_id": "Profile One",
            "targets": [
                "https://www.bilibili.com/video/BVdetail001?p=2",
                "https://www.bilibili.com/video/BVdetail001?p=3",
            ],
        }
    )
    service._futures[run["id"]].result(timeout=3)
    saved = service.get_run(run["id"])
    candidates = service.list_candidates(run["id"])
    assert saved["state"] == "completed"
    assert saved["provider_id"] == "cbce_bilibili"
    assert visited == run["request"]["targets"]
    assert [item["part_index"] for item in candidates["items"]] == [2, 3]
    assert len({item["id"] for item in candidates["items"]}) == 2
    service.shutdown()


def test_unimplemented_platform_is_rejected_by_acquisition_capability_gate(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        service.create_run(
            {
                "mode": "video",
                "targets": ["https://www.tiktok.com/@creator/video/123456789"],
            }
        )
    except AcquisitionError as error:
        assert error.code == "UNSUPPORTED_OPERATION"
        assert "tiktok" in str(error).casefold()
    else:
        raise AssertionError("unverified platform must not enter acquisition")
    service.shutdown()


def test_configured_tiktok_display_supports_metadata_only_video_and_creator(tmp_path):
    store = SQLiteStore(tmp_path / "tiktok-display.db")
    store.initialize()
    downloads = FakeDownloads()

    class FakeTikTokConnector:
        configured = True

        @staticmethod
        def _item(external_id: str, title: str):
            return SimpleNamespace(
                external_id=external_id,
                canonical_url=f"https://www.tiktok.com/@game.dev/video/{external_id}",
                title=title,
                author="game.dev",
                published_at=None,
                metrics={"view_count": 10},
            )

        async def scan_channel(self, channel, query):
            assert channel["normalized_url"] == "https://www.tiktok.com/@game.dev"
            assert query.max_items == 2
            yield self._item("7123456789012345678", "Authorized creator video")

        async def fetch_detail(self, target_url):
            assert target_url.endswith("/7123456789012345678")
            return self._item("7123456789012345678", "Authorized detail")

    service = AcquisitionManager(
        store,
        downloads,
        max_workers=1,
        connectors={"tiktok": FakeTikTokConnector()},
    )
    try:
        creator_run = service.create_run(
            {
                "mode": "creator",
                "targets": ["https://www.tiktok.com/@game.dev"],
                "limits": {"max_candidates": 2},
            }
        )
        service._futures[creator_run["id"]].result(timeout=3)
        creator_candidates = service.list_candidates(creator_run["id"])["items"]
        assert service.get_run(creator_run["id"])["provider_id"] == "tiktok_display"
        assert creator_candidates[0]["source_id"] == "tiktok"
        assert creator_candidates[0]["provider_id"] == "tiktok_display"
        assert creator_candidates[0]["download_available"] is False

        selection = service.select_downloads([creator_candidates[0]["id"]])
        assert downloads.calls == []
        intent = store.acquisition_document(
            "acquisition_download_intents", selection["intent_ids"][0]
        )
        assert intent["state"] == "failed"
        assert "media download contract" in intent["error"]

        detail_run = service.create_run(
            {
                "mode": "video",
                "targets": [
                    "https://www.tiktok.com/@game.dev/video/7123456789012345678"
                ],
            }
        )
        service._futures[detail_run["id"]].result(timeout=3)
        assert service.get_run(detail_run["id"])["provider_id"] == "tiktok_display"
        assert service.get_run(detail_run["id"])["state"] == "completed"
    finally:
        service.shutdown()


def test_tiktok_display_capability_keeps_download_search_and_playlist_closed(tmp_path):
    store = SQLiteStore(tmp_path / "tiktok-capability.db")
    store.initialize()

    class ConfiguredTikTokConnector:
        configured = True

        async def scan_channel(self, _channel, _query):
            if False:
                yield None

        async def fetch_detail(self, _target):
            return None

    service = AcquisitionManager(
        store,
        FakeDownloads(),
        connectors={"tiktok": ConfiguredTikTokConnector()},
    )
    try:
        operations = {
            key: value
            for key, value in service.capabilities()["items"][2]["operations"].items()
        }
        assert operations["video"]["status"] == "unverified"
        assert operations["creator"]["status"] == "unverified"
        assert operations["search"]["status"] == "unsupported"
        assert operations["playlist"]["status"] == "unsupported"
        assert operations["download"]["status"] == "unsupported"
    finally:
        service.shutdown()


def test_tiktok_display_never_falls_back_to_ytdlp_when_unconfigured(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        with pytest.raises(AcquisitionError, match="TikTok") as error:
            service.create_run(
                {
                    "mode": "video",
                    "provider_id": "yt-dlp",
                    "targets": [
                        "https://www.tiktok.com/@creator/video/123456789"
                    ],
                }
            )
        assert error.value.code == "UNSUPPORTED_OPERATION"
    finally:
        service.shutdown()


def test_configured_bilibili_creator_connector_normalizes_to_acquisition_candidate(tmp_path):
    store = SQLiteStore(tmp_path / "creator.db")
    store.initialize()
    downloads = FakeDownloads()

    class FakeCreatorConnector:
        configured = True

        async def list_creator(self, _target_url, *, max_items=20):
            assert max_items == 2
            yield SimpleNamespace(
                external_id="BVcreator001",
                canonical_url="https://www.bilibili.com/video/BVcreator001",
                title="Creator video",
                author="Creator",
                published_at=None,
                metrics={"view_count": 7},
            )

    service = AcquisitionManager(
        store,
        downloads,
        max_workers=1,
        connectors={"bilibili": FakeCreatorConnector()},
    )
    run = service.create_run(
        {
            "mode": "creator",
            "targets": ["https://space.bilibili.com/2"],
            "limits": {"max_candidates": 2},
        }
    )
    service._futures[run["id"]].result(timeout=3)
    detail = service.get_run(run["id"])
    page = service.list_candidates(run["id"])
    assert detail["provider_id"] == "cbce_bilibili"
    assert detail["state"] == "completed"
    assert page["items"][0]["title"] == "Creator video"
    assert page["items"][0]["provider_id"] == "cbce_bilibili"
    service.shutdown()


def test_bilibili_connection_id_is_routed_to_connector_profile_factory(tmp_path):
    store = SQLiteStore(tmp_path / "creator-connection.db")
    store.initialize()
    downloads = FakeDownloads()

    class FakeCreatorConnector:
        configured = True

        def __init__(self, account_ref="default", calls=None):
            self.account_ref = account_ref
            self.calls = calls if calls is not None else []

        def for_account(self, account_ref):
            self.calls.append(account_ref)
            return FakeCreatorConnector(account_ref, self.calls)

        async def list_creator(self, _target_url, *, max_items=20):
            assert self.account_ref == "account one"
            assert max_items == 1
            yield SimpleNamespace(
                external_id="BVconnection001",
                canonical_url="https://www.bilibili.com/video/BVconnection001",
                title="Connection video",
                author="Creator",
                published_at=None,
                metrics={},
            )

    connector = FakeCreatorConnector()
    service = AcquisitionManager(
        store,
        downloads,
        max_workers=1,
        connectors={"bilibili": connector},
    )
    run = service.create_run(
        {
            "mode": "creator",
            "connection_id": " Account One ",
            "targets": ["https://space.bilibili.com/2"],
            "limits": {"max_candidates": 1},
        }
    )
    service._futures[run["id"]].result(timeout=3)

    assert connector.calls == ["account one"]
    assert service.get_run(run["id"])["request"]["connection_id"] == "account one"
    assert service.list_candidates(run["id"])["items"][0]["title"] == "Connection video"
    service.shutdown()


def test_bilibili_connection_id_is_carried_into_download_selection(tmp_path):
    store, downloads, service = manager(tmp_path)
    bili_url = "https://www.bilibili.com/video/BVdownload001"
    bili_candidate = candidate(bili_url)
    bili_candidate.update(source_id="bilibili", provider_id="yt-dlp")
    service._extractor = lambda _request: [bili_candidate]
    run = service.create_run({"mode": "video", "targets": [bili_url]})
    service._futures[run["id"]].result(timeout=3)

    selection = service.select_downloads(
        ["candidate-1"],
        quality="720",
        connection_id=" Account One ",
    )

    assert selection["connection_id"] == "account one"
    assert downloads.jobs[0]["connection_id"] == "account one"
    intent = store.acquisition_document(
        "acquisition_download_intents", selection["intent_ids"][0]
    )
    assert intent["connection_id"] == "account one"
    assert ":connection:" in str(intent.get("_id", intent.get("id")))
    service.shutdown()


def test_configured_bilibili_search_connector_is_explicitly_opt_in(tmp_path):
    store = SQLiteStore(tmp_path / "search.db")
    store.initialize()
    downloads = FakeDownloads()

    class FakeSearchConnector:
        configured = True
        spec = SimpleNamespace(provider_id="cbce_bilibili")

        async def search(self, query):
            assert query.name == "cats"
            yield SimpleNamespace(
                external_id="BVsearch001",
                canonical_url="https://www.bilibili.com/video/BVsearch001",
                title="Search video",
                author="Search creator",
                published_at=None,
                metrics={"view_count": 3},
            )

    service = AcquisitionManager(
        store,
        downloads,
        max_workers=1,
        connectors={"bilibili": FakeSearchConnector()},
    )
    run = service.create_run(
        {
            "mode": "search",
            "query": "cats",
            "source_id": "bilibili",
            "provider_id": "cbce_bilibili",
            "limits": {"max_candidates": 2},
        }
    )
    service._futures[run["id"]].result(timeout=3)
    detail = service.get_run(run["id"])
    page = service.list_candidates(run["id"])
    assert detail["provider_id"] == "cbce_bilibili"
    assert detail["state"] == "completed"
    assert page["items"][0]["title"] == "Search video"
    service.shutdown()


def test_ytdlp_metadata_keeps_bilibili_part_selector(monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _target, download=False):
            assert download is False
            assert self.options["noplaylist"] is True
            return {
                "id": "BV1abc12345",
                "title": "Part two",
                "webpage_url": "https://www.bilibili.com/video/BV1abc12345",
                "duration": 42,
                "vcodec": "avc1",
            }

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    rows = AcquisitionManager._extract_with_yt_dlp(
        {
            "mode": "video",
            "targets": ["https://www.bilibili.com/video/BV1abc12345?p=2"],
            "limits": {"max_candidates": 20, "deadline_seconds": 30},
        }
    )
    assert rows[0]["canonical_url"].endswith("?p=2")
    assert rows[0]["media_id"] == "BV1abc12345:p2"


def test_ytdlp_bilibili_playlist_flat_entries_resolve_content_urls(monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, target, download=False):
            assert target.endswith("/medialist/detail/ml213003412")
            assert download is False
            assert self.options["noplaylist"] is False
            return {
                "entries": [
                    {
                        "id": "BV1oT4y1F7dX",
                        "url": "https://www.bilibili.com/video/BV1oT4y1F7dX",
                        "title": "First",
                        "playlist_index": 1,
                    },
                    {
                        "id": "BV1Jy4y1y7pa",
                        "title": "Second",
                        "playlist_index": 2,
                    },
                ]
            }

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    rows = AcquisitionManager._extract_with_yt_dlp(
        {
            "mode": "playlist",
            "targets": ["https://www.bilibili.com/medialist/detail/ml213003412"],
            "limits": {"max_candidates": 20, "deadline_seconds": 30},
        }
    )
    assert [row["canonical_url"] for row in rows] == [
        "https://www.bilibili.com/video/BV1oT4y1F7dX",
        "https://www.bilibili.com/video/BV1Jy4y1y7pa",
    ]
    assert [row["listing_position"] for row in rows] == [1, 2]


def test_connector_item_entry_carries_cover_and_publish_time_to_candidate() -> None:
    item = SimpleNamespace(
        external_id="BV1oT4y1F7dX",
        canonical_url="https://www.bilibili.com/video/BV1oT4y1F7dX",
        title="Preview item",
        author="Public creator",
        published_at=datetime(2026, 8, 13, 14, 25, tzinfo=UTC),
        metrics={"view_count": 12},
        media=[{"kind": "cover", "url": "https://i.example.com/cover.jpg"}],
    )

    entry = AcquisitionManager._connector_item_entry(
        item,
        provider_id="cbce_bilibili",
    )
    candidate = AcquisitionManager._normalize_entry(
        entry,
        fallback_url=item.canonical_url,
        fallback_source="bilibili",
        position=1,
    )

    assert candidate["thumbnail_url"] == "https://i.example.com/cover.jpg"
    assert candidate["published_at"] == "2026-08-13T14:25:00+00:00"


def test_bilibili_playlist_enrichment_is_bounded_and_redacts_no_session_data(monkeypatch):
    payload = {
        "code": 0,
        "data": {
            "medias": [
                {
                    "bvid": "BV1oT4y1F7dX",
                    "title": "Public title",
                    "pubtime": 1_600_000_000,
                    "duration": 42,
                    "cover": "https://i.example.com/cover.jpg",
                    "upper": {"mid": 84912, "name": "Public creator"},
                    "cnt_info": {"play": 12, "like": 3, "reply": 2},
                }
            ],
            "has_more": False,
        },
    }
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(payload).encode("utf-8")

    def open_url(request, *, timeout):
        calls.append((request.full_url, timeout, dict(request.header_items())))
        return Response()

    monkeypatch.setattr(acquisition_module, "urlopen", open_url)
    result = AcquisitionManager._bilibili_playlist_metadata(
        "https://www.bilibili.com/medialist/detail/ml213003412",
        max_candidates=20,
        deadline_seconds=30,
        started=time.monotonic(),
    )
    assert result["BV1oT4y1F7dX"]["title"] == "Public title"
    assert result["BV1oT4y1F7dX"]["duration"] == 42
    assert result["BV1oT4y1F7dX"]["thumbnail"] == "https://i.example.com/cover.jpg"
    assert result["BV1oT4y1F7dX"]["timestamp"] == 1_600_000_000
    assert len(calls) == 1
    assert "cookie" not in calls[0][2]
    assert "media_id=213003412" in calls[0][0]


def test_ytdlp_youtube_search_builds_canonical_url_from_flat_entry(monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, target, download=False):
            assert target.startswith("ytsearch2:")
            assert download is False
            return {
                "entries": [
                    {"id": "dQw4w9WgXcQ", "title": "Search result"},
                ]
            }

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    rows = AcquisitionManager._extract_with_yt_dlp(
        {
            "mode": "search",
            "query": "python tutorial",
            "source_id": "youtube",
            "limits": {"max_candidates": 2, "deadline_seconds": 30},
        }
    )
    assert rows[0]["canonical_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert rows[0]["external_id"] == "dQw4w9WgXcQ"


def test_saved_channel_is_idempotent_and_rejects_video_target(tmp_path):
    store, _, service = manager(tmp_path)
    first = service.save_channel("https://www.youtube.com/@creator", label="Creator")
    second = service.save_channel("https://www.youtube.com/@creator", label="Updated")
    assert first["id"] == second["id"]
    assert second["label"] == "Updated"
    assert len(service.list_channels()) == 1
    try:
        service.save_channel("https://www.youtube.com/watch?v=abc123")
    except AcquisitionError as error:
        assert error.code == "INVALID_REQUEST"
    else:
        raise AssertionError("a video URL must not be saved as a channel")
    assert store.acquisition_document("acquisition_channels", first["id"]) is not None
    service.shutdown()


def test_saved_bilibili_playlist_aliases_share_a_stable_channel_identity(tmp_path):
    _, _, service = manager(tmp_path)
    try:
        first = service.save_channel(
            "https://space.bilibili.com/123/favlist?fid=1103407912&ftype=create",
            label="Favorites",
        )
        second = service.save_channel(
            "https://www.bilibili.com/list/ml1103407912",
            label="Playlist",
        )
        assert first["id"] == second["id"]
        assert second["target_kind"] == "playlist"
        assert second["canonical_url"] == (
            "https://www.bilibili.com/medialist/detail/ml1103407912"
        )
        assert service.list_channels()[0]["label"] == "Playlist"
    finally:
        service.shutdown()


def test_channel_patch_and_delete_stop_tracking_without_deleting_source_data(tmp_path):
    store, _, service = manager(tmp_path)
    try:
        channel = service.save_channel("https://space.bilibili.com/2", connection_id="Main Profile")
        renamed = service.update_channel(
            channel["id"], label="Bilibili chính", connection_id="Second Profile"
        )
        assert renamed is not None
        assert renamed["label"] == "Bilibili chính"
        assert renamed["connection_id"] == "second profile"

        cleared = service.update_channel(channel["id"], connection_id=None)
        assert cleared is not None
        assert "connection_id" not in cleared

        assert service.delete_channel(channel["id"]) is True
        assert service.delete_channel(channel["id"]) is True
        assert service.list_channels() == []
        stored = store.acquisition_document("acquisition_channels", channel["id"])
        assert stored is not None
        assert stored["enabled"] is False
        assert stored["subscription"]["enabled"] is False
    finally:
        service.shutdown()


def test_saved_bilibili_channel_persists_connection_for_scheduler(tmp_path):
    store, _, service = manager(tmp_path)

    class FakeCreatorConnector:
        configured = True

        def for_account(self, _account_ref):
            return self

        async def list_creator(self, _target_url, *, max_items=20):
            del max_items
            if False:
                yield None

    service._bilibili_connector = FakeCreatorConnector()
    service._subscription_capability = lambda _channel: {"enabled": True}
    requests = []
    service._extractor = lambda request: requests.append(request) or []

    channel = service.save_channel(
        "https://space.bilibili.com/2",
        connection_id=" Account One ",
    )
    assert channel["connection_id"] == "account one"
    service.update_subscription(channel["id"], enabled=True)
    service.scheduler_tick()

    for _ in range(100):
        if requests:
            break
        time.sleep(0.01)
    assert requests[0]["connection_id"] == "account one"
    assert store.acquisition_document("acquisition_channels", channel["id"])[
        "connection_id"
    ] == "account one"
    service.shutdown()


def test_subscription_baselines_then_only_auto_downloads_new_candidate(tmp_path):
    _store, downloads, service = manager(tmp_path)
    channel = service.save_channel("https://www.youtube.com/@creator")
    service.update_subscription(channel["id"], enabled=True, auto_download=True, quality="720")
    calls = [0]

    def extract(_request):
        calls[0] += 1
        if calls[0] == 1:
            return [candidate()]
        new_video = candidate(
            "https://www.youtube.com/watch?v=new456", "candidate-2"
        )
        new_video["published_at"] = service._now()
        historical = candidate("https://www.youtube.com/watch?v=old456", "old")
        historical["published_at"] = "2020-01-01T00:00:00+00:00"
        unknown = candidate("https://www.youtube.com/watch?v=unknown", "unknown")
        return [candidate(candidate_id="candidate-1"), historical, unknown, new_video]

    service._extractor = extract
    service.scheduler_tick()
    for _ in range(100):
        if service.list_channels()[0]["subscription"].get("baseline_initialized"):
            break
        time.sleep(0.01)
    first_channel = service.list_channels()[0]
    assert first_channel["subscription"]["baseline_initialized"] is True
    assert service.store.acquisition_document(
        "acquisition_subscription_observations", f"{channel['id']}:candidate-1"
    )["classification"] == "baseline"
    assert downloads.calls == []

    due = service.store.acquisition_document("acquisition_channels", channel["id"])
    due["subscription"]["next_run_at"] = "2020-01-01T00:00:00+00:00"
    service.store.upsert_acquisition_document("acquisition_channels", due)
    service.scheduler_tick()
    for _ in range(100):
        if downloads.calls:
            break
        time.sleep(0.01)
    assert service.store.acquisition_document(
        "acquisition_subscription_observations", f"{channel['id']}:candidate-2"
    )["classification"] == "new"
    assert downloads.calls == [["https://www.youtube.com/watch?v=new456"]]
    for candidate_id, classification in (("old", "historical"), ("unknown", "unknown")):
        assert service.store.acquisition_document(
            "acquisition_subscription_observations", f"{channel['id']}:{candidate_id}"
        )["classification"] == classification
    service.shutdown()


def test_subscription_auto_download_is_deferred_across_feature_flag_pause(tmp_path, monkeypatch):
    _store, downloads, service = manager(tmp_path)
    channel = service.save_channel("https://www.youtube.com/@creator")
    service.update_subscription(channel["id"], enabled=True, auto_download=True)
    calls = [0]
    entered = Event()
    release = Event()

    def extract(_request):
        calls[0] += 1
        if calls[0] == 1:
            return [candidate()]
        new_video = candidate(
            "https://www.youtube.com/watch?v=deferred", "candidate-deferred"
        )
        new_video["published_at"] = service._now()
        if calls[0] == 2:
            entered.set()
            assert release.wait(5)
        return [candidate(), new_video]

    service._extractor = extract
    try:
        service.scheduler_tick()
        for _ in range(100):
            if service.list_channels()[0]["subscription"].get("baseline_initialized"):
                break
            time.sleep(0.01)

        due = service.store.acquisition_document("acquisition_channels", channel["id"])
        due["subscription"]["next_run_at"] = "2020-01-01T00:00:00+00:00"
        service.store.upsert_acquisition_document("acquisition_channels", due)
        service.scheduler_tick()
        assert entered.wait(3)
        monkeypatch.setattr(acquisition_module.settings, "content_bot_acquisition_enabled", False)
        release.set()
        for _ in range(100):
            subscription = service.list_channels()[0]["subscription"]
            if subscription.get("last_status") == "deferred_feature":
                break
            time.sleep(0.01)
        deferred = service.list_channels()[0]["subscription"]
        assert deferred["deferred_auto_download_ids"] == ["candidate-deferred"]
        assert downloads.calls == []

        monkeypatch.setattr(acquisition_module.settings, "content_bot_acquisition_enabled", True)
        due = service.store.acquisition_document("acquisition_channels", channel["id"])
        due["subscription"]["next_run_at"] = "2020-01-01T00:00:00+00:00"
        service.store.upsert_acquisition_document("acquisition_channels", due)
        service.scheduler_tick()
        for _ in range(100):
            subscription = service.list_channels()[0]["subscription"]
            if downloads.calls and subscription.get("deferred_auto_download_ids") == []:
                break
            time.sleep(0.01)
        assert downloads.calls == [["https://www.youtube.com/watch?v=deferred"]]
        assert service.list_channels()[0]["subscription"]["deferred_auto_download_ids"] == []
    finally:
        release.set()
        service.shutdown()


def test_overlapping_scheduler_ticks_do_not_create_duplicate_subscription_runs(tmp_path):
    _store, _downloads, service = manager(
        tmp_path,
        extractor=lambda _request: [candidate()],
    )
    channel = service.save_channel("https://www.youtube.com/@creator")
    service.update_subscription(channel["id"], enabled=True)
    barrier = Barrier(2)

    def tick() -> None:
        barrier.wait(timeout=2)
        service.scheduler_tick()

    threads = [Thread(target=tick) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    for _ in range(100):
        if service.list_channels()[0]["subscription"].get("baseline_initialized"):
            break
        time.sleep(0.01)
    assert len(service.list_runs()) == 1
    service.shutdown()


def test_subscription_catches_up_after_application_restart(tmp_path):
    store, _downloads, service = manager(tmp_path, extractor=lambda _request: [])
    try:
        channel = service.save_channel("https://www.youtube.com/@creator")
        service.update_subscription(channel["id"], enabled=True)
        old_run_id = "subscription-run-before-restart"
        store.upsert_acquisition_document(
            "acquisition_runs",
            {
                "_id": old_run_id,
                "state": "running",
                "request": {
                    "mode": "creator",
                    "targets": ["https://www.youtube.com/@creator"],
                    "subscription_id": channel["id"],
                },
                "created_at": service._now(),
            },
        )
        channel_record = store.acquisition_document(
            "acquisition_channels", channel["id"]
        )
        channel_record["subscription"].update(
            active_run_id=old_run_id,
            lease_expires_at="2099-01-01T00:00:00+00:00",
        )
        store.upsert_acquisition_document("acquisition_channels", channel_record)

        service.start()
        service.scheduler_tick()
        service.scheduler_tick()
        for _ in range(100):
            if (
                len(service.list_runs()) >= 2
                and service.list_channels()[0]["subscription"].get("last_status")
                == "succeeded"
            ):
                break
            time.sleep(0.01)
        runs = {run["id"]: run for run in service.list_runs()}
        assert runs[old_run_id]["state"] == "paused"
        assert len(runs) == 2
        current = service.list_channels()[0]["subscription"]
        assert current["last_status"] == "succeeded"
        assert current["last_run_id"] != old_run_id
        assert current["active_run_id"] is None
    finally:
        service.shutdown()


@pytest.mark.parametrize("auto_download", [False, True])
def test_subscription_backfill_and_replay_preserve_download_intent(tmp_path, auto_download):
    store, downloads, service = manager(tmp_path)
    try:
        channel = service.save_channel("https://www.youtube.com/@creator")
        service.update_subscription(
            channel["id"], enabled=True, initial_policy="backfill", auto_download=auto_download,
        )
        store.upsert_acquisition_document("acquisition_candidates", candidate())
        store.upsert_acquisition_document("acquisition_runs", {
            "_id": "backfill-run", "state": "completed", "created_at": service._now(),
        })
        store.upsert_acquisition_document("acquisition_run_items", {
            "_id": "backfill-run:candidate-1", "run_id": "backfill-run",
            "candidate_id": "candidate-1", "position": 1,
        })
        channel_record = store.acquisition_document("acquisition_channels", channel["id"])
        channel_record["subscription"]["active_run_id"] = "backfill-run"
        channel_record["subscription"]["lease_expires_at"] = "2099-01-01T00:00:00+00:00"
        store.upsert_acquisition_document("acquisition_channels", channel_record)
        service._finalize_subscription(channel["id"], "backfill-run")
        service._finalize_subscription(channel["id"], "backfill-run")
        assert len(downloads.jobs) == (1 if auto_download else 0)
        assert len(store.acquisition_documents("acquisition_selections")) == (1 if auto_download else 0)
        observation = store.acquisition_document(
            "acquisition_subscription_observations", f"{channel['id']}:candidate-1",
        )
        assert observation["classification"] == "backfill"
    finally:
        service.shutdown()


def test_scheduler_does_not_claim_unverified_bilibili_creator_channel(tmp_path):
    store = SQLiteStore(tmp_path / "bilibili-subscription.db")
    store.initialize()
    downloads = FakeDownloads()

    class FakeCreatorConnector:
        configured = True

        async def list_creator(self, _target_url, *, max_items=20):
            del max_items
            if False:
                yield None

    service = AcquisitionManager(
        store,
        downloads,
        connectors={"bilibili": FakeCreatorConnector()},
    )
    channel = service.save_channel("https://space.bilibili.com/2")
    service.update_subscription(channel["id"], enabled=True)
    service.scheduler_tick()

    saved = service.list_channels()[0]["subscription"]
    assert service.list_runs() == []
    assert saved["last_status"] == "waiting_capability"
    assert saved["active_run_id"] is None
    assert "hydrate gate" in saved["last_error"].lower()
    service.shutdown()


def test_subscription_storage_claim_is_single_use_and_expires(tmp_path, mongo_store):
    sqlite = SQLiteStore(tmp_path / "claim.db")
    sqlite.initialize()
    for store in (sqlite, mongo_store):
        store.upsert_acquisition_document(
            "acquisition_channels",
            {
                "_id": "claim-channel",
                "subscription": {
                    "enabled": True,
                    "next_run_at": "2026-09-21T10:00:00+00:00",
                },
            },
        )
        first = store.claim_acquisition_channel(
            "claim-channel",
            run_id="run-1",
            now="2026-09-21T10:00:00+00:00",
            lease_expires_at="2026-09-21T10:15:00+00:00",
        )
        assert first["subscription"]["active_run_id"] == "run-1"
        assert store.claim_acquisition_channel(
            "claim-channel",
            run_id="run-2",
            now="2026-09-21T10:01:00+00:00",
            lease_expires_at="2026-09-21T10:16:00+00:00",
        ) is None
        expired = store.claim_acquisition_channel(
            "claim-channel",
            run_id="run-3",
            now="2026-09-21T10:16:00+00:00",
            lease_expires_at="2026-09-21T10:31:00+00:00",
        )
        assert expired["subscription"]["active_run_id"] == "run-3"


def test_subscription_reconciliation_claim_is_single_use_and_expires(tmp_path, mongo_store):
    sqlite = SQLiteStore(tmp_path / "reconcile-claim.db")
    sqlite.initialize()
    for store in (sqlite, mongo_store):
        store.upsert_acquisition_document(
            "acquisition_channels",
            {
                "_id": "reconcile-channel",
                "subscription": {
                    "enabled": True,
                    "active_run_id": "run-1",
                },
            },
        )
        first = store.claim_acquisition_reconciliation(
            "reconcile-channel",
            run_id="run-1",
            now="2026-09-21T10:00:00+00:00",
            lease_expires_at="2026-09-21T10:05:00+00:00",
        )
        assert first["subscription"]["reconcile_run_id"] == "run-1"
        first_token = first["subscription"]["reconcile_lease_token"]
        assert store.claim_acquisition_reconciliation(
            "reconcile-channel",
            run_id="run-1",
            now="2026-09-21T10:01:00+00:00",
            lease_expires_at="2026-09-21T10:06:00+00:00",
        ) is None
        expired = store.claim_acquisition_reconciliation(
            "reconcile-channel",
            run_id="run-1",
            now="2026-09-21T10:05:00+00:00",
            lease_expires_at="2026-09-21T10:10:00+00:00",
        )
        assert expired["subscription"]["reconcile_run_id"] == "run-1"
        replacement_token = expired["subscription"]["reconcile_lease_token"]
        assert replacement_token != first_token
        assert store.complete_acquisition_reconciliation(
            "reconcile-channel",
            run_id="run-1",
            lease_token=first_token,
            now="2026-09-21T10:06:00+00:00",
            updates={"last_status": "stale-owner"},
        ) is None
        completed = store.complete_acquisition_reconciliation(
            "reconcile-channel",
            run_id="run-1",
            lease_token=replacement_token,
            now="2026-09-21T10:06:00+00:00",
            updates={
                "active_run_id": None,
                "reconcile_run_id": None,
                "last_status": "succeeded",
            },
        )
        assert completed["subscription"]["last_status"] == "succeeded"
        assert store.complete_acquisition_reconciliation(
            "reconcile-channel",
            run_id="run-1",
            lease_token=replacement_token,
            now="2026-09-21T10:07:00+00:00",
            updates={"last_status": "failed"},
        ) is None


def test_expired_reconciliation_cannot_complete_without_takeover(tmp_path, mongo_store):
    sqlite = SQLiteStore(tmp_path / "expired-reconciliation.db")
    sqlite.initialize()
    for store in (sqlite, mongo_store):
        store.upsert_acquisition_document("acquisition_channels", {
            "_id": "expired-channel",
            "subscription": {"enabled": True, "active_run_id": "run-1"},
        })
        claimed = store.claim_acquisition_reconciliation(
            "expired-channel", run_id="run-1",
            now="2026-09-21T10:00:00+00:00",
            lease_expires_at="2026-09-21T10:05:00+00:00",
        )
        assert store.complete_acquisition_reconciliation(
            "expired-channel", run_id="run-1",
            lease_token=claimed["subscription"]["reconcile_lease_token"],
            now="2026-09-21T10:05:00+00:00",
            updates={"active_run_id": None, "last_status": "succeeded"},
        ) is None
        assert store.acquisition_document("acquisition_channels", "expired-channel")[
            "subscription"
        ]["active_run_id"] == "run-1"


def test_selection_reservation_is_single_winner(tmp_path, mongo_store):
    sqlite = SQLiteStore(tmp_path / "selection-reservation.db")
    sqlite.initialize()
    for store in (sqlite, mongo_store):
        first = store.reserve_acquisition_selection(
            {
                "_id": "selection-winner-1",
                "idempotency_key": "same-selection",
                "state": "queued",
            }
        )
        second = store.reserve_acquisition_selection(
            {
                "_id": "selection-winner-2",
                "idempotency_key": "same-selection",
                "state": "queued",
            }
        )
        assert first["id"] == second["id"] == "selection-winner-1"
        assert len(
            [
                row
                for row in store.acquisition_documents("acquisition_selections")
                if row.get("idempotency_key") == "same-selection"
            ]
        ) == 1


def test_selection_reservation_is_atomic_across_sqlite_store_instances(tmp_path):
    path = tmp_path / "selection-reservation-race.db"
    first_store = SQLiteStore(path)
    second_store = SQLiteStore(path)
    first_store.initialize()
    second_store.initialize()
    barrier = Barrier(2)
    results: list[dict] = []

    def reserve(store: SQLiteStore, identifier: str) -> None:
        barrier.wait(timeout=3)
        results.append(
            store.reserve_acquisition_selection(
                {
                    "_id": identifier,
                    "idempotency_key": "cross-process-selection",
                    "state": "queued",
                }
            )
        )

    threads = [
        Thread(target=reserve, args=(first_store, "selection-race-1")),
        Thread(target=reserve, args=(second_store, "selection-race-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert len(results) == 2
    assert len({row["id"] for row in results}) == 1
    assert len(
        [
            row
            for row in first_store.acquisition_documents("acquisition_selections")
            if row.get("idempotency_key") == "cross-process-selection"
        ]
    ) == 1


def test_acquisition_http_contract_is_wired_to_application_services(application_services):
    application_services.acquisition._extractor = lambda _request: [candidate()]
    with TestClient(main.app) as client:
        capabilities = client.get("/api/v1/acquisition/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["items"][0]["source_id"] == "youtube"
        created = client.post(
            "/api/v1/acquisition/runs",
            json={
                "mode": "video",
                "targets": ["https://www.youtube.com/watch?v=abc123"],
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        for _ in range(100):
            detail = client.get(f"/api/v1/acquisition/runs/{run_id}").json()
            if detail["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        page = client.get(f"/api/v1/acquisition/runs/{run_id}/candidates")
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 1


def test_acquisition_http_selection_group_controls(application_services):
    fake_downloads = FakeDownloads()
    application_services.acquisition.video_downloads = fake_downloads
    application_services.acquisition._extractor = lambda _request: [candidate()]
    with TestClient(main.app) as client:
        created = client.post(
            "/api/v1/acquisition/runs",
            json={
                "mode": "video",
                "targets": ["https://www.youtube.com/watch?v=abc123"],
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        for _ in range(100):
            if client.get(f"/api/v1/acquisition/runs/{run_id}").json()["state"] == "completed":
                break
            time.sleep(0.01)
        selected = client.post(
            "/api/v1/acquisition/download-selections",
            json={
                "candidate_ids": ["candidate-1"],
                "quality": "720",
                "idempotency_key": "http-selection-control",
            },
        )
        assert selected.status_code == 202, selected.text
        selection_id = selected.json()["id"]
        paused = client.post(f"/api/v1/acquisition/download-selections/{selection_id}/pause")
        assert paused.status_code == 200, paused.text
        resumed = client.post(
            f"/api/v1/acquisition/download-selections/{selection_id}/resume",
            json={},
        )
        assert resumed.status_code == 202, resumed.text
        canceled = client.post(f"/api/v1/acquisition/download-selections/{selection_id}/cancel")
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["state"] == "canceled"


def test_acquisition_http_channel_patch_and_delete(application_services):
    with TestClient(main.app) as client:
        created = client.post(
            "/api/v1/acquisition/channels",
            json={"url": "https://www.youtube.com/@creator", "label": "Old label"},
        )
        assert created.status_code == 201, created.text
        channel_id = created.json()["id"]

        patched = client.patch(
            f"/api/v1/acquisition/channels/{channel_id}",
            json={"label": "New label"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["label"] == "New label"

        deleted = client.delete(f"/api/v1/acquisition/channels/{channel_id}")
        assert deleted.status_code == 204, deleted.text
        listed = client.get("/api/v1/acquisition/channels")
        assert listed.status_code == 200, listed.text
        assert channel_id not in {item["id"] for item in listed.json()["items"]}


def test_acquisition_http_feature_flag_contract(application_services, monkeypatch):
    monkeypatch.setattr(acquisition_module.settings, "content_bot_acquisition_enabled", False)
    with TestClient(main.app) as client:
        capabilities = client.get("/api/v1/acquisition/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["feature_enabled"] is False

        response = client.post(
            "/api/v1/acquisition/runs",
            json={
                "mode": "video",
                "targets": ["https://www.youtube.com/watch?v=abc123"],
            },
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "FEATURE_DISABLED"
        assert detail["retry_after"] is None
        assert detail["run_id"] is None
        assert detail["job_id"] is None
