import asyncio
import uuid
from datetime import UTC, datetime

from app.services.connectors import RawContentItem
from app.services.runs import EventBus, RunManager


def add_keyword(storage, name: str, include_terms=None, exclude_terms=None) -> int:
    now = datetime.now(UTC)
    return storage.create_keyword(
        {
            "name": name,
            "normalized_name": name.casefold(),
            "include_terms": include_terms or [],
            "exclude_terms": exclude_terms or [],
            "source_ids": ["fake"],
            "enabled": False,
            "interval_minutes": 360,
            "max_items_per_source": 500,
            "next_run_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )["id"]


def add_item(storage, external_id: str, title: str) -> dict:
    now = datetime.now(UTC)
    return storage.save_item(
        {
            "source_id": "fake",
            "external_id": external_id,
            "canonical_url": f"https://example.test/{external_id}",
            "title": title,
            "body_snippet": "",
            "author": "",
            "hashtags": [],
            "locale": None,
            "published_at": None,
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )


def test_ingest_rejects_zero_relevance_and_deduplicates(mongo_store) -> None:
    batch_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    keyword_id = add_keyword(mongo_store, "NTE")
    mongo_store.create_batch(
        {"id": batch_id, "keyword_id": keyword_id, "trigger": "manual", "state": "running"},
        [{"id": source_run_id, "batch_id": batch_id, "source_id": "fake", "state": "running", "fetched_count": 0, "ingested_count": 0}],
    )
    manager = RunManager({}, EventBus(), mongo_store)

    async def ingest(raw: RawContentItem) -> None:
        await manager._ingest(source_run_id, keyword_id, ["NTE"], ["giveaway"], raw)

    asyncio.run(ingest(RawContentItem("irrelevant", "https://example.test/1", "A content update")))
    asyncio.run(ingest(RawContentItem("excluded", "https://example.test/2", "NTE giveaway")))
    accepted = RawContentItem("accepted", "https://example.test/3", "NTE gameplay", metrics={"like_count": 2})
    asyncio.run(ingest(accepted))
    asyncio.run(ingest(accepted))

    source_run = mongo_store.source_run(source_run_id)
    assert source_run["fetched_count"] == 4
    assert source_run["ingested_count"] == 2
    assert mongo_store.db.content_items.count_documents({}) == 1
    assert mongo_store.db.item_keyword_matches.count_documents({}) == 1
    assert mongo_store.db.metric_snapshots.count_documents({}) == 2

    asyncio.run(ingest(RawContentItem("accepted", "https://example.test/3", "NTE giveaway")))
    source_run = mongo_store.source_run(source_run_id)
    assert source_run["fetched_count"] == 5
    assert source_run["ingested_count"] == 2
    assert mongo_store.db.content_items.count_documents({}) == 0
    assert mongo_store.db.item_keyword_matches.count_documents({}) == 0
    assert mongo_store.db.metric_snapshots.count_documents({}) == 0


def test_cleanup_irrelevant_preserves_items_with_another_valid_match(mongo_store) -> None:
    first_id = add_keyword(mongo_store, "First")
    second_id = add_keyword(mongo_store, "Second")
    orphan = add_item(mongo_store, "orphan", "Orphan")
    shared = add_item(mongo_store, "shared", "Shared")
    mongo_store.save_match(orphan["id"], first_id, {"relevance_score": 0})
    mongo_store.save_match(shared["id"], first_id, {"relevance_score": 0})
    mongo_store.save_match(shared["id"], second_id, {"relevance_score": 40})

    assert RunManager({}, EventBus(), mongo_store).cleanup_irrelevant() == (2, 1)
    assert mongo_store.db.content_items.count_documents({}) == 1
    assert mongo_store.db.item_keyword_matches.count_documents({}) == 1


def test_rescore_keyword_applies_edited_aliases_and_exclusions(mongo_store) -> None:
    keyword_id = add_keyword(mongo_store, "Primary Name", ["Hades 2"], ["giveaway"])
    excluded = add_item(mongo_store, "rescore-excluded", "Hades 2 giveaway")
    accepted = add_item(mongo_store, "rescore-accepted", "Hades 2 update")
    mongo_store.save_match(excluded["id"], keyword_id, {"relevance_score": 20})
    mongo_store.save_match(accepted["id"], keyword_id, {"relevance_score": 20})

    assert RunManager({}, EventBus(), mongo_store).rescore_keyword(keyword_id) == (2, 1)
    assert mongo_store.db.content_items.count_documents({}) == 1
    match = mongo_store.db.item_keyword_matches.find_one()
    assert match["relevance_score"] == 40
    assert match["match_reasons"] == ["title: Hades 2"]
