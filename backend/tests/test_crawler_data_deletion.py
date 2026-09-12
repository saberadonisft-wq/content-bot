from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import main
from app.services.runs import EventBus, RunManager


def test_source_data_deletion_is_confirmed_canonical_and_cascading(
    application_services,
    mongo_store,
    monkeypatch,
) -> None:
    monkeypatch.setattr(application_services, "store", mongo_store)
    monkeypatch.setattr(application_services.run_manager, "store", mongo_store)
    now = datetime.now(UTC)
    keyword = mongo_store.create_keyword(
        {
            "name": "Delete Bilibili observations",
            "normalized_name": "delete bilibili observations",
            "include_terms": [],
            "exclude_terms": [],
            "source_ids": ["bilibili"],
            "source_checkpoints": {"bilibili": {"search": {"cursor": "old"}}},
            "channels": [
                {
                    "id": "channel-1",
                    "source_id": "bilibili",
                    "checkpoint": {"cursor": "old"},
                    "last_scanned_at": now,
                    "last_status": "succeeded",
                    "last_error": None,
                }
            ],
            "enabled": False,
            "interval_minutes": 360,
            "max_items_per_source": 10,
            "next_run_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    item = mongo_store.save_item(
        {
            "source_id": "bilibili",
            "external_id": "BV-delete",
            "canonical_url": "https://www.bilibili.com/video/BV-delete",
            "title": "Delete me",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    mongo_store.save_match(item["id"], keyword["id"], {"relevance_score": 50})
    mongo_store.add_snapshot(
        {"content_item_id": item["id"], "captured_at": now, "view_count": 1}
    )
    mongo_store.save_comment(
        {
            "source_id": "bilibili",
            "external_id": "comment-delete",
            "content_external_id": "BV-delete",
            "body": "Delete this comment too",
            "published_at": now,
            "like_count": 0,
            "child_count": 0,
            "root_external_id": "comment-delete",
            "provenance": {},
        }
    )
    mongo_store.db.source_runs.insert_one(
        {
            "_id": "source-run-delete",
            "batch_id": "batch-delete",
            "source_id": "bilibili",
            "state": "succeeded",
        }
    )

    with TestClient(main.app) as client:
        rejected = client.post(
            "/api/v1/crawler-data/bili/delete",
            json={"confirmation": "yes", "delete_profiles": False},
        )
        assert rejected.status_code == 422
        assert mongo_store.item(item["id"]) is not None

        response = client.post(
            "/api/v1/crawler-data/bili/delete",
            json={
                "confirmation": "DELETE bilibili DATA",
                "delete_profiles": False,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "source_id": "bilibili",
        "content_items": 1,
        "comments": 1,
        "item_matches": 1,
        "metric_snapshots": 1,
        "source_runs": 1,
        "keywords_reset": 1,
        "profiles_deleted": 0,
    }
    updated = mongo_store.keyword(keyword["id"])
    assert "bilibili" not in updated["source_checkpoints"]
    assert updated["channels"][0]["checkpoint"] == {}
    assert updated["channels"][0]["last_status"] is None


def test_automatic_retention_cascades_database_rows_only(
    mongo_store,
    tmp_path,
) -> None:
    now = datetime.now(UTC)
    old = mongo_store.save_item(
        {
            "source_id": "instagram",
            "external_id": "old",
            "canonical_url": "https://www.instagram.com/p/Old12/",
            "title": "Old",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now - timedelta(days=100),
            "last_seen_at": now - timedelta(days=100),
        }
    )
    current = mongo_store.save_item(
        {
            "source_id": "instagram",
            "external_id": "current",
            "canonical_url": "https://www.instagram.com/p/New12/",
            "title": "Current",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    mongo_store.add_snapshot(
        {"content_item_id": old["id"], "captured_at": now, "view_count": 1}
    )
    profile_sentinel = tmp_path / "browser-profiles-v2" / "sentinel"
    profile_sentinel.parent.mkdir(parents=True)
    profile_sentinel.write_text("must survive automatic retention", encoding="utf-8")

    manager = RunManager({}, EventBus(), mongo_store)
    assert manager.cleanup_retention(90) == 1
    assert mongo_store.item(old["id"]) is None
    assert mongo_store.snapshots(old["id"]) == []
    assert mongo_store.item(current["id"]) is not None
    assert profile_sentinel.exists()
