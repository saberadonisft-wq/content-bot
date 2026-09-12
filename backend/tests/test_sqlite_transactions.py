from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event

import pytest

from app.sqlite_store import SQLiteStore


def test_checkpoints_survive_overlapping_store_instances(tmp_path, monkeypatch):
    first = SQLiteStore(tmp_path / "shared.db")
    first.initialize()
    second = SQLiteStore(first.path)
    keyword = first.create_keyword({"name": "Game", "source_checkpoints": {}})
    first_read, second_started, second_read = Event(), Event(), Event()
    get_first, get_second = first._get, second._get

    def paused_first(collection, identifier):
        row = get_first(collection, identifier)
        if collection == "keywords":
            first_read.set()
            assert second_started.wait(5)
            # Old code lets the second writer read stale data here. A transaction
            # makes it wait until this writer commits, so don't require that read.
            second_read.wait(0.2)
        return row

    def observed_second(collection, identifier):
        row = get_second(collection, identifier)
        second_read.set()
        return row

    monkeypatch.setattr(first, "_get", paused_first)
    monkeypatch.setattr(second, "_get", observed_second)
    now = datetime.now(UTC)

    def update_second():
        assert first_read.wait(5)
        second_started.set()
        second.update_source_checkpoint(
            keyword["id"], "web", "search", {"n": 2}, updated_at=now
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(
            first.update_source_checkpoint,
            keyword["id"],
            "youtube",
            "search",
            {"n": 1},
            updated_at=now,
        )
        two = pool.submit(update_second)
        one.result(timeout=10)
        two.result(timeout=10)
    actual = second.keyword(keyword["id"])["source_checkpoints"]
    assert actual == {"youtube": {"search": {"n": 1}}, "web": {"search": {"n": 2}}}


@pytest.mark.parametrize("workers", [2, 8])
def test_session_numbers_are_atomic_across_instances(tmp_path, workers):
    path = tmp_path / "shared.db"
    stores = [SQLiteStore(path) for _ in range(workers)]
    stores[0].initialize()
    keyword = stores[0].create_keyword({"name": "Game"})

    def increment(index):
        return stores[index % len(stores)].next_session_number(keyword["id"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        numbers = list(pool.map(increment, range(40)))
    assert sorted(numbers) == list(range(1, 41))
    assert stores[0].keyword(keyword["id"])["session_count"] == 40


def test_eight_sources_preserve_checkpoint_progress_on_one_keyword(tmp_path):
    stores = [SQLiteStore(tmp_path / "sources.db") for _ in range(8)]
    stores[0].initialize()
    keyword = stores[0].create_keyword({"name": "Game"})
    gate = Barrier(8)
    now = datetime.now(UTC)

    def update(index):
        gate.wait(timeout=10)
        for progress in range(8):
            stores[index].update_source_checkpoint(
                keyword["id"],
                f"source-{index}",
                "search",
                {"cursor": progress},
                updated_at=now,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(8)))
    assert stores[0].keyword(keyword["id"])["source_checkpoints"] == {
        f"source-{index}": {"search": {"cursor": 7}} for index in range(8)
    }


def test_identity_upsert_is_unique_across_store_instances(tmp_path):
    stores = [SQLiteStore(tmp_path / "identities.db") for _ in range(4)]
    stores[0].initialize()

    def save(index):
        store = stores[index % len(stores)]
        item = store.save_item({"source_id": "web", "external_id": "shared"})
        match = store.save_match(item["id"], 1, {"relevance_score": index + 1})
        return item["id"], match["id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(save, range(24)))
    assert len(set(ids)) == 1
    assert len(stores[0]._all("content_items")) == 1
    assert len(stores[0]._all("item_keyword_matches")) == 1


def test_failed_bundle_rolls_back_item_snapshot_match_and_counters(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "rollback.db")
    store.initialize()
    upsert = store._upsert_with

    def fail_after_match(connection, collection, identifier, document):
        upsert(connection, collection, identifier, document)
        if collection == "item_keyword_matches":
            raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(store, "_upsert_with", fail_after_match)
    with pytest.raises(RuntimeError, match="simulated"):
        store.ingest_content_bundle(
            {"source_id": "web", "external_id": "a"},
            {"captured_at": datetime.now(UTC)},
            {"relevance_score": 80},
            1,
        )
    for collection in ("content_items", "metric_snapshots", "item_keyword_matches"):
        assert store._all(collection) == []
        assert store.next_id(collection) == 1


def test_bundle_callback_reads_uncommitted_snapshot_on_its_own_connection(tmp_path):
    store = SQLiteStore(tmp_path / "visibility.db")
    store.initialize()
    other = SQLiteStore(store.path)

    def score(item):
        assert store.item(item["id"])["external_id"] == "a"
        assert len(store.snapshots(item["id"])) == 1
        assert other.item(item["id"]) is None
        return 42

    saved = store.ingest_content_bundle(
        {"source_id": "web", "external_id": "a"},
        {"captured_at": datetime.now(UTC)},
        {"relevance_score": 80},
        1,
        trend_score_fn=score,
    )
    assert other.match(saved["id"], 1)["trend_score"] == 42


def test_connections_close_at_operation_boundary_without_gc(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "handles.db")
    connect = store._connect
    connections = []

    def track():
        connection = connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", track)
    store.initialize()
    keyword = store.create_keyword({"name": "Game"})
    store.keyword(keyword["id"])
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
