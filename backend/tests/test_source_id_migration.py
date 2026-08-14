from datetime import UTC, datetime

from scripts import migrate_source_ids_v2 as migration


def test_alias_migration_is_dry_run_first_and_updates_canonical_fields(
    mongo_store, monkeypatch
) -> None:
    monkeypatch.setattr(migration, "store", mongo_store)
    now = datetime.now(UTC)
    keyword_id = mongo_store.create_keyword(
        {
            "name": "Alias migration",
            "normalized_name": "alias migration",
            "source_ids": ["dy", "ks", "douyin"],
            "channels": [{"id": "old", "source_id": "bili"}],
            "source_checkpoints": {"wb": {"search": {"schema_version": 1}}},
            "created_at": now,
            "updated_at": now,
        }
    )["id"]
    mongo_store.db.content_items.insert_one(
        {"_id": 100, "source_id": "dy", "external_id": "video-1"}
    )
    mongo_store.db.source_runs.insert_one(
        {"_id": "run-1", "source_id": "ks"}
    )

    summary = migration.plan()
    assert summary["content_items"]["dy"] == 1
    assert mongo_store.db.content_items.find_one({"_id": 100})["source_id"] == "dy"

    changed = migration.apply_migration(summary)
    assert changed["content_items"]["dy"] == 1
    keyword = mongo_store.keyword(keyword_id)
    assert keyword["source_ids"] == ["douyin", "kuaishou"]
    assert keyword["channels"][0]["source_id"] == "bilibili"
    assert "weibo" in keyword["source_checkpoints"]
    assert "wb" not in keyword["source_checkpoints"]


def test_alias_migration_refuses_unique_key_collision(mongo_store, monkeypatch) -> None:
    monkeypatch.setattr(migration, "store", mongo_store)
    mongo_store.db.content_items.insert_many(
        [
            {"_id": 1, "source_id": "dy", "external_id": "same"},
            {"_id": 2, "source_id": "douyin", "external_id": "same"},
        ]
    )
    summary = migration.plan()
    assert summary["collisions"]

    try:
        migration.apply_migration(summary)
    except RuntimeError as exc:
        assert "No documents were changed" in str(exc)
    else:
        raise AssertionError("migration should refuse colliding IDs")
    assert mongo_store.db.content_items.find_one({"_id": 1})["source_id"] == "dy"
