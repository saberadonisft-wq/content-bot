from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app import main


def test_delete_keyword_removes_orphans_and_preserves_shared_items(
    application_services, mongo_store, monkeypatch
) -> None:
    monkeypatch.setattr(application_services, "store", mongo_store)
    monkeypatch.setattr(application_services.run_manager, "store", mongo_store)
    now = datetime.now(UTC)
    base = {
        "include_terms": [],
        "exclude_terms": [],
        "source_ids": [],
        "enabled": False,
        "interval_minutes": 360,
        "max_items_per_source": 500,
        "next_run_at": None,
        "created_at": now,
        "updated_at": now,
    }
    first_id = mongo_store.create_keyword(
        {
            **base,
            "name": "First delete target",
            "normalized_name": "first delete target",
        }
    )["id"]
    second_id = mongo_store.create_keyword(
        {**base, "name": "Second survivor", "normalized_name": "second survivor"}
    )["id"]
    item_base = {
        "source_id": "fake",
        "title": "",
        "body_snippet": "",
        "author": "",
        "hashtags": [],
        "metrics": {},
        "raw_payload": {},
        "first_seen_at": now,
        "last_seen_at": now,
    }
    orphan = mongo_store.save_item(
        {
            **item_base,
            "external_id": "orphan-delete",
            "canonical_url": "https://example.test/1",
        }
    )
    shared = mongo_store.save_item(
        {
            **item_base,
            "external_id": "shared-delete",
            "canonical_url": "https://example.test/2",
        }
    )
    mongo_store.save_match(orphan["id"], first_id, {"relevance_score": 40})
    mongo_store.save_match(shared["id"], first_id, {"relevance_score": 40})
    mongo_store.save_match(shared["id"], second_id, {"relevance_score": 40})

    with TestClient(main.app) as client:
        assert client.delete(f"/api/v1/keywords/{first_id}").status_code == 204

    assert mongo_store.keyword(first_id) is None
    assert mongo_store.keyword(second_id) is not None
    assert mongo_store.db.content_items.count_documents({}) == 1
    assert mongo_store.db.item_keyword_matches.count_documents({}) == 1
    assert mongo_store.db.content_items.find_one()["external_id"] == "shared-delete"
