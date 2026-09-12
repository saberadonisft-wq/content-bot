"""Behavior shared by the default SQLite store and the optional Mongo adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.sqlite_store import SQLiteStore

NOW = datetime(2026, 9, 12, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "mongo"])
def storage(request, tmp_path, mongo_store):
    if request.param == "mongo":
        return mongo_store
    store = SQLiteStore(tmp_path / "contract.db")
    store.initialize()
    return store


def item(store, external="a", source="web"):
    return store.save_item(
        {
            "source_id": source,
            "external_id": external,
            "title": "Game",
            "canonical_url": f"https://example.com/{external}",
            "metrics": {},
            "first_seen_at": NOW,
            "last_seen_at": NOW,
        }
    )


def test_match_identity_and_filters(storage):
    one, two, zero = item(storage), item(storage, "b", "youtube"), item(storage, "c")
    original = storage.save_match(
        one["id"], 1, {"relevance_score": 50, "session_id": "old"}
    )
    updated = storage.save_match(
        one["id"], 1, {"relevance_score": 80, "session_id": "new"}
    )
    storage.save_match(two["id"], 1, {"relevance_score": 60, "session_id": "new"})
    storage.save_match(zero["id"], 1, {"relevance_score": 0, "session_id": "new"})
    storage.save_match(one["id"], 2, {"relevance_score": 90})
    assert updated["id"] == original["id"]
    assert {i["id"] for i, _ in storage.item_matches(1, positive_only=True)} == {
        one["id"],
        two["id"],
    }
    assert [i["id"] for i, _ in storage.item_matches(1, min_relevance=70)] == [
        one["id"]
    ]
    assert [i["id"] for i, _ in storage.item_matches(1, source_id="youtube")] == [
        two["id"]
    ]
    assert storage.item_matches(1, session_id="old") == []
    assert len(storage.item_matches(1, session_id="new")) == 3


def test_snapshot_order_limit_and_idempotence(storage):
    saved = item(storage)
    for seconds in (3, 1, 2):
        storage.add_snapshot(
            {
                "content_item_id": saved["id"],
                "captured_at": NOW + timedelta(seconds=seconds),
                "like_count": seconds,
            }
        )
    storage.add_snapshot(
        {
            "content_item_id": saved["id"],
            "captured_at": NOW + timedelta(seconds=2),
            "like_count": 20,
        }
    )
    assert [s["like_count"] for s in storage.snapshots(saved["id"])] == [1, 20, 3]
    assert [
        s["like_count"]
        for s in storage.snapshots(saved["id"], descending=True, limit=2)
    ] == [3, 20]


def test_keyword_delete_preserves_shared_content(storage):
    first = storage.create_keyword({"name": "One", "normalized_name": "one"})
    second = storage.create_keyword({"name": "Two", "normalized_name": "two"})
    shared, orphan = item(storage), item(storage, "orphan")
    for keyword in (first, second):
        storage.save_match(shared["id"], keyword["id"], {"relevance_score": 90})
    storage.save_match(orphan["id"], first["id"], {"relevance_score": 80})
    assert storage.delete_keyword(first["id"])
    assert storage.item(shared["id"]) is not None
    assert storage.item(orphan["id"]) is None
    assert storage.match(shared["id"], second["id"]) is not None


def test_source_and_channel_checkpoints_preserve_siblings(storage):
    keyword = storage.create_keyword(
        {
            "name": "Game",
            "normalized_name": "game",
            "channels": [{"id": "one"}, {"id": "two"}],
        }
    )
    for source in ("youtube", "web"):
        storage.update_source_checkpoint(
            keyword["id"], source, "search", {"cursor": source}, updated_at=NOW
        )
    for channel in ("one", "two"):
        storage.update_channel_checkpoint(
            keyword["id"],
            channel,
            {"cursor": channel},
            scanned_at=NOW,
            status="succeeded",
        )
    actual = storage.keyword(keyword["id"])
    assert set(actual["source_checkpoints"]) == {"youtube", "web"}
    assert [c["checkpoint"]["cursor"] for c in actual["channels"]] == ["one", "two"]


def test_mongo_bundle_failure_exposes_documented_partial_write(
    mongo_store, monkeypatch
):
    def fail(_values):
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(mongo_store, "add_snapshot", fail)
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        mongo_store.ingest_content_bundle(
            {"source_id": "web", "external_id": "a"},
            {"captured_at": NOW},
            {"relevance_score": 80},
            1,
        )
    saved = mongo_store.item_by_source("web", "a")
    assert saved is not None
    assert mongo_store.match(saved["id"], 1) is None


def test_bulk_snapshot_pairs_equal_individual_reads(storage):
    from datetime import timezone

    from app.storage_protocol import PersistenceStore

    assert isinstance(storage, PersistenceStore)
    one, two, other = (
        item(storage),
        item(storage, "b"),
        item(storage, "other", "youtube"),
    )
    for saved in (one, two, other):
        for index in (3, 1, 2):
            captured = NOW + timedelta(hours=index)
            if index == 2:
                captured = captured.astimezone(timezone(timedelta(hours=7)))
            storage.add_snapshot(
                {
                    "content_item_id": saved["id"],
                    "captured_at": captured,
                    "like_count": index,
                }
            )
    single = item(storage, "single")
    storage.add_snapshot(
        {"content_item_id": single["id"], "captured_at": NOW, "like_count": 1}
    )
    actual = {
        pair[0]["content_item_id"]: pair
        for pair in storage.source_snapshot_pairs("web")
    }
    assert set(actual) == {one["id"], two["id"]}
    for identifier, pair in actual.items():
        assert pair == storage.snapshots(identifier, descending=True, limit=2)
