from app.services import insight_cache
from app.services.insight_cache import cached_aggregate
from app.services.item_query import ItemQuery
from app.sqlite_store import SQLiteStore


def test_cache_invalidates_across_store_instances_and_returns_independent_values(tmp_path):
    first = SQLiteStore(tmp_path / "cache.db")
    first.initialize()
    second = SQLiteStore(first.path)
    calls = []

    def compute():
        calls.append(1)
        return {"total": first.query_items(ItemQuery(1))[0], "nested": []}

    one = cached_aggregate(first, ItemQuery(1), ("summary",), compute)
    one["nested"].append("consumer edit")
    assert cached_aggregate(first, ItemQuery(1), ("summary",), compute) == {"total": 0, "nested": []}
    assert len(calls) == 1
    item = second.save_item({"source_id": "web", "external_id": "1"})
    second.save_match(item["id"], 1, {"relevance_score": 80})
    assert cached_aggregate(first, ItemQuery(1), ("summary",), compute)["total"] == 1
    second.delete_item(item["id"])
    assert cached_aggregate(first, ItemQuery(1), ("summary",), compute)["total"] == 0
    assert len(calls) == 3


def test_cache_does_not_store_results_while_revision_changes(tmp_path):
    store = SQLiteStore(tmp_path / "changing.db")
    store.initialize()
    calls = []

    def compute():
        calls.append(1)
        store.save_item({"source_id": "web", "external_id": str(len(calls))})
        return {"total": len(calls)}

    for expected in (1, 2):
        assert cached_aggregate(store, ItemQuery(1), ("summary",), compute)["total"] == expected


def test_cache_expiry_and_entry_budget(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "bounded.db")
    store.initialize()
    now = [0]
    monkeypatch.setattr(insight_cache, "monotonic", lambda: now[0])
    monkeypatch.setattr(insight_cache, "_MAX_ENTRIES", 2)
    calls = []

    def compute():
        calls.append(1)
        return {"call": len(calls)}

    for keyword in (1, 2, 3, 1):
        cached_aggregate(store, ItemQuery(keyword), ("summary",), compute)
    assert len(calls) == 4  # First keyword was evicted.
    now[0] = 31
    assert cached_aggregate(store, ItemQuery(1), ("summary",), compute)["call"] == 5
