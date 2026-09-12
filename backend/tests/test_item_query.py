from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main, sqlite_item_query
from app.services import item_query
from app.services.item_query import ItemQuery, query_items
from app.sqlite_store import SQLiteStore

NOW = datetime(2026, 9, 12, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "mongo"])
def query_store(request, tmp_path, mongo_store):
    store = (
        mongo_store if request.param == "mongo" else SQLiteStore(tmp_path / "items.db")
    )
    store.initialize()
    store.create_keyword({"name": "Game", "normalized_name": "game"})
    for index in range(12):
        item = store.save_item(
            {
                "source_id": "web" if index % 2 else "youtube",
                "external_id": str(index),
                "canonical_url": f"https://example.com/{index}",
                "title": "Amazing patch release" if index % 3 else "Terrible crash bug",
                "body_snippet": "Straße",
                "author": f"Author {index}",
                "locale": "en",
                "published_at": NOW + timedelta(seconds=index // 2),
                "first_seen_at": NOW,
                "last_seen_at": NOW,
            }
        )
        store.save_match(
            item["id"],
            1,
            {
                "relevance_score": 0 if index == 0 else 40 + index * 5,
                "trend_score": index // 4,
                "session_id": "a" if index < 6 else "b",
            },
        )
    return store


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"query": "  STRASSE "},
        {"query": "Author 11"},
        {"query": "absent"},
        {"source_id": "web", "session_id": "b", "min_relevance": 80},
        {"language": "en", "sentiment": "negative", "topic": "bugs"},
        {"source_id": "", "session_id": "", "language": ""},
    ],
)
def test_filters_and_pages_match_fallback(query_store, filters):
    query = ItemQuery(1, **filters)
    expected_total, expected = query_items(query_store, query, indexed=False)
    actual_total, actual = query_items(query_store, query)
    assert (actual_total, actual) == (expected_total, expected)
    for offset in (0, 2, 20):
        total, page = query_items(query_store, query, limit=2, offset=offset)
        assert total == expected_total
        assert page == expected[offset : offset + 2]


def test_api_list_summary_clusters_exports_share_filters(
    application_services, query_store, monkeypatch
):
    monkeypatch.setattr(application_services, "store", query_store)
    with TestClient(main.app) as client:
        filters = {
            "keyword_id": 1,
            "query": "patch",
            "source_id": "web",
            "min_relevance": 60,
        }
        items = client.get("/api/v1/items", params=filters).json()
        assert items["total"] > 0
        for endpoint in ("summary", "clusters"):
            response = client.get(f"/api/v1/insights/{endpoint}", params=filters)
            assert response.status_code == 200, response.text
            assert response.json()["total_items"] == items["total"]
        exported = client.get("/api/v1/export.json", params=filters).json()
        assert [row["url"] for row in exported] == [
            row["canonical_url"] for row in items["items"]
        ]
        response = client.get("/api/v1/export.csv", params=filters)
        assert len(list(csv.DictReader(io.StringIO(response.text)))) == items["total"]


def test_sql_page_only_decodes_requested_items(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "page.db")
    store.initialize()
    with store._transaction():
        for index in range(100):
            item = store.save_item(
                {
                    "source_id": "web",
                    "external_id": str(index),
                    "canonical_url": "https://example.com",
                    "first_seen_at": NOW,
                    "last_seen_at": NOW,
                }
            )
            store.save_match(item["id"], 1, {"relevance_score": 80})
    original = store._loads
    decoded = []
    monkeypatch.setattr(
        store, "_loads", lambda payload: (decoded.append(payload), original(payload))[1]
    )
    total, page = query_items(store, ItemQuery(1), limit=5, offset=20)
    assert total == 100 and len(page) == 5
    assert len(decoded) == 10  # Five item documents and their five match documents.


def test_sql_analysis_reuses_metrics_updates_and_invalidates_content(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "analysis.db")
    store.initialize()
    item = store.save_item(
        {"source_id": "web", "external_id": "1", "title": "Amazing game"}
    )
    store.save_match(item["id"], 1, {"relevance_score": 80})
    calls = []
    analyze = sqlite_item_query.analyze_item
    monkeypatch.setattr(
        sqlite_item_query,
        "analyze_item",
        lambda value: (calls.append(value), analyze(value))[1],
    )
    store.save_item({"id": item["id"], "metrics": {"view_count": 10}})
    assert not calls
    store.save_item({"id": item["id"], "title": "Terrible crash bug"})
    assert len(calls) == 1
    assert store.query_items(ItemQuery(1, sentiment="negative"))[0] == 1
    assert store.query_items(ItemQuery(1, sentiment="positive"))[0] == 0
    store.delete_item(item["id"])
    assert store.query_items(ItemQuery(1))[0] == 0
    with store._connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM local_item_analysis").fetchone()[0]
            == 0
        )


def test_legacy_write_between_refresh_and_snapshot_is_rebuilt(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "legacy-writer.db")
    store.initialize()
    item = store.save_item(
        {"source_id": "web", "external_id": "1", "title": "Amazing game"}
    )
    store.save_match(item["id"], 1, {"relevance_score": 80})
    original = sqlite_item_query.refresh_item_analysis
    once = False

    def refresh_then_legacy_write(target):
        nonlocal once
        original(target)
        if not once:
            once = True
            with store._connection() as connection:
                connection.execute(
                    "UPDATE local_documents SET payload=json_set(payload, '$.title', ?) WHERE document_key=?",
                    ("Terrible crash bug", f"i:{item['id']}"),
                )

    monkeypatch.setattr(
        sqlite_item_query, "refresh_item_analysis", refresh_then_legacy_write
    )
    assert store.query_items(ItemQuery(1, sentiment="negative"))[0] == 1
    assert store.query_items(ItemQuery(1, sentiment="positive"))[0] == 0


def test_analysis_version_rebuild_and_cache_values_are_independent(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "version.db")
    store.initialize()
    item = {"source_id": "web", "external_id": "1", "title": "Amazing game"}
    store.save_item(item)
    one = item_query.analyze_item(item)
    one["sentiment"]["label"] = "changed by response consumer"
    assert (
        item_query.analyze_item(item)["sentiment"]["label"] != one["sentiment"]["label"]
    )
    monkeypatch.setattr(sqlite_item_query, "ANALYSIS_VERSION", "test-v2")
    store.initialize()
    sqlite_item_query.refresh_item_analysis(store)
    with store._connection() as connection:
        assert (
            connection.execute("SELECT version FROM local_item_analysis").fetchone()[0]
            == "test-v2"
        )


def test_mongo_legacy_backfill_and_atomic_analysis_update(mongo_store, monkeypatch):
    from app.mongo_item_query import PROJECTION_FIELD

    collection = mongo_store.db.content_items
    collection.insert_one(
        {"_id": 1, "source_id": "web", "external_id": "1", "title": "Amazing game"}
    )
    mongo_store.save_match(1, 1, {"relevance_score": 80})
    assert mongo_store.query_items(ItemQuery(1, sentiment="positive"))[0] == 1
    assert PROJECTION_FIELD not in mongo_store.item(1)
    original = collection.update_one
    once = False

    def concurrent_title_change(selector, update, **kwargs):
        nonlocal once
        if not once:
            once = True
            # Simulates a writer committing after save_item reads its inputs.
            original({"_id": 1}, {"$set": {"title": "Terrible crash bug"}})
        return original(selector, update, **kwargs)

    monkeypatch.setattr(collection, "update_one", concurrent_title_change)
    mongo_store.save_item({"id": 1, "metrics": {"view_count": 9}})
    assert mongo_store.item(1)["title"] == "Terrible crash bug"
    assert mongo_store.query_items(ItemQuery(1, sentiment="positive"))[0] == 0
    assert mongo_store.query_items(ItemQuery(1, sentiment="negative"))[0] == 1
