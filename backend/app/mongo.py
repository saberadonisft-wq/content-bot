from __future__ import annotations

from copy import deepcopy
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database

from .config import settings


class MongoStore:
    """MongoDB persistence for the API, scheduler, and ingestion pipeline."""

    def __init__(self, database: Database | None = None) -> None:
        if database is not None:
            self.client = database.client
            self.db = database
            self._ping_enabled = False
            return
        if not settings.mongodb_uri:
            raise RuntimeError("MONGODB_URI is required")
        self.client = MongoClient(
            settings.mongodb_uri,
            tz_aware=True,
            serverSelectionTimeoutMS=10_000,
            connectTimeoutMS=10_000,
        )
        self.db = self.client[settings.mongodb_database]
        self._ping_enabled = True

    def initialize(self, ping: bool = True) -> None:
        if ping and self._ping_enabled:
            self.client.admin.command("ping")
        self.db.keywords.create_index("normalized_name", unique=True)
        self.db.keywords.create_index([("updated_at", DESCENDING)])
        self.db.keywords.create_index([("enabled", ASCENDING), ("next_run_at", ASCENDING)])
        self.db.content_items.create_index([("source_id", ASCENDING), ("external_id", ASCENDING)], unique=True)
        self.db.content_items.create_index([("source_id", ASCENDING), ("published_at", DESCENDING)])
        self.db.content_items.create_index("last_seen_at")
        self.db.item_keyword_matches.create_index(
            [("content_item_id", ASCENDING), ("keyword_id", ASCENDING)], unique=True
        )
        self.db.item_keyword_matches.create_index([("keyword_id", ASCENDING), ("trend_score", DESCENDING)])
        self.db.metric_snapshots.create_index(
            [("content_item_id", ASCENDING), ("captured_at", ASCENDING)], unique=True
        )
        self.db.crawl_batches.create_index([("keyword_id", ASCENDING), ("started_at", DESCENDING)])
        self.db.crawl_batches.create_index("state")
        self.db.source_runs.create_index("batch_id")

    def next_id(self, collection: str) -> int:
        row = self.db.counters.find_one_and_update(
            {"_id": collection},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(row["value"])

    @staticmethod
    def public(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        if doc is None:
            return None
        result = deepcopy(doc)
        result["id"] = result.pop("_id")
        return result

    def keyword(self, keyword_id: int) -> dict[str, Any] | None:
        return self.public(self.db.keywords.find_one({"_id": keyword_id}))

    def keywords(self) -> list[dict[str, Any]]:
        return [self.public(row) for row in self.db.keywords.find().sort("updated_at", DESCENDING)]

    def keyword_name_exists(self, normalized_name: str, exclude_id: int | None = None) -> bool:
        query: dict[str, Any] = {"normalized_name": normalized_name}
        if exclude_id is not None:
            query["_id"] = {"$ne": exclude_id}
        return self.db.keywords.find_one(query, {"_id": 1}) is not None

    def create_keyword(self, values: dict[str, Any]) -> dict[str, Any]:
        row = {"_id": self.next_id("keywords"), **values}
        self.db.keywords.insert_one(row)
        return self.public(row)

    def update_keyword(self, keyword_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        row = self.db.keywords.find_one_and_update(
            {"_id": keyword_id}, {"$set": values}, return_document=ReturnDocument.AFTER
        )
        return self.public(row)

    def delete_keyword(self, keyword_id: int) -> bool:
        if self.db.keywords.delete_one({"_id": keyword_id}).deleted_count == 0:
            return False
        batch_ids = [row["_id"] for row in self.db.crawl_batches.find({"keyword_id": keyword_id}, {"_id": 1})]
        if batch_ids:
            self.db.source_runs.delete_many({"batch_id": {"$in": batch_ids}})
            self.db.crawl_batches.delete_many({"_id": {"$in": batch_ids}})
        content_ids = list(
            {
                row["content_item_id"]
                for row in self.db.item_keyword_matches.find({"keyword_id": keyword_id}, {"content_item_id": 1})
            }
        )
        self.db.item_keyword_matches.delete_many({"keyword_id": keyword_id})
        if content_ids:
            still_matched = set(
                row["content_item_id"]
                for row in self.db.item_keyword_matches.find(
                    {"content_item_id": {"$in": content_ids}},
                    {"content_item_id": 1},
                )
            )
            orphan_ids = [cid for cid in content_ids if cid not in still_matched]
            if orphan_ids:
                self.db.content_items.delete_many({"_id": {"$in": orphan_ids}})
                self.db.metric_snapshots.delete_many({"content_item_id": {"$in": orphan_ids}})
        return True

    def create_batch(self, batch: dict[str, Any], source_runs: list[dict[str, Any]]) -> None:
        self.db.crawl_batches.insert_one({"_id": batch.pop("id"), **batch})
        if source_runs:
            self.db.source_runs.insert_many([{"_id": row.pop("id"), **row} for row in source_runs])

    def active_batch(self, keyword_id: int) -> dict[str, Any] | None:
        row = self.db.crawl_batches.find_one(
            {"keyword_id": keyword_id, "state": {"$in": ["queued", "running"]}},
            sort=[("started_at", DESCENDING)],
        )
        return self.public(row)

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.public(self.db.crawl_batches.find_one({"_id": batch_id}))
        if row is not None:
            row["source_runs"] = self.source_runs(batch_id)
        return row

    def batches(self, keyword_id: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
        query = {} if keyword_id is None else {"keyword_id": keyword_id}
        rows = self.db.crawl_batches.find(query).sort("started_at", DESCENDING).limit(limit)
        result = []
        for raw in rows:
            row = self.public(raw)
            row["source_runs"] = self.source_runs(row["id"])
            result.append(row)
        return result

    def update_batch(self, batch_id: str, values: dict[str, Any]) -> None:
        self.db.crawl_batches.update_one({"_id": batch_id}, {"$set": values})

    def source_run(self, source_run_id: str) -> dict[str, Any] | None:
        return self.public(self.db.source_runs.find_one({"_id": source_run_id}))

    def source_runs(self, batch_id: str) -> list[dict[str, Any]]:
        return [self.public(row) for row in self.db.source_runs.find({"batch_id": batch_id})]

    def update_source_run(self, source_run_id: str, values: dict[str, Any], increments: dict[str, int] | None = None) -> None:
        update: dict[str, Any] = {}
        if values:
            update["$set"] = values
        if increments:
            update["$inc"] = increments
        if not update:
            return
        self.db.source_runs.update_one({"_id": source_run_id}, update)

    def item_by_source(self, source_id: str, external_id: str) -> dict[str, Any] | None:
        return self.public(self.db.content_items.find_one({"source_id": source_id, "external_id": external_id}))

    def item(self, content_item_id: int) -> dict[str, Any] | None:
        return self.public(self.db.content_items.find_one({"_id": content_item_id}))

    def save_item(self, values: dict[str, Any]) -> dict[str, Any]:
        values = deepcopy(values)
        content_item_id = values.pop("id", None)
        if content_item_id is None:
            content_item_id = self.next_id("content_items")
        self.db.content_items.update_one({"_id": content_item_id}, {"$set": values}, upsert=True)
        return {"id": content_item_id, **values}

    def delete_item(self, content_item_id: int) -> None:
        self.db.content_items.delete_one({"_id": content_item_id})
        self.db.item_keyword_matches.delete_many({"content_item_id": content_item_id})
        self.db.metric_snapshots.delete_many({"content_item_id": content_item_id})

    def match(self, content_item_id: int, keyword_id: int) -> dict[str, Any] | None:
        return self.public(
            self.db.item_keyword_matches.find_one(
                {"content_item_id": content_item_id, "keyword_id": keyword_id}
            )
        )

    def save_match(self, content_item_id: int, keyword_id: int, values: dict[str, Any]) -> dict[str, Any]:
        row = self.db.item_keyword_matches.find_one_and_update(
            {"content_item_id": content_item_id, "keyword_id": keyword_id},
            {"$set": values, "$setOnInsert": {"_id": self.next_id("item_keyword_matches")}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return self.public(row)

    def delete_match(self, content_item_id: int, keyword_id: int) -> None:
        self.db.item_keyword_matches.delete_one(
            {"content_item_id": content_item_id, "keyword_id": keyword_id}
        )
        if self.db.item_keyword_matches.find_one({"content_item_id": content_item_id}, {"_id": 1}) is None:
            self.delete_item(content_item_id)

    def add_snapshot(self, values: dict[str, Any]) -> None:
        self.db.metric_snapshots.update_one(
            {"content_item_id": values["content_item_id"], "captured_at": values["captured_at"]},
            {"$set": values},
            upsert=True,
        )

    def snapshots(self, content_item_id: int, descending: bool = False, limit: int = 0) -> list[dict[str, Any]]:
        direction = DESCENDING if descending else ASCENDING
        cursor = self.db.metric_snapshots.find({"content_item_id": content_item_id}).sort("captured_at", direction)
        if limit:
            cursor = cursor.limit(limit)
        return [self.public(row) for row in cursor]

    def item_matches(self, keyword_id: int, positive_only: bool = False) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        query: dict[str, Any] = {"keyword_id": keyword_id}
        if positive_only:
            query["relevance_score"] = {"$gt": 0}
        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for raw_match in self.db.item_keyword_matches.find(query):
            item = self.item(raw_match["content_item_id"])
            if item is not None:
                rows.append((item, self.public(raw_match)))
        return rows

    def source_metric_values(self, source_id: str) -> list[dict[str, int]]:
        return [row.get("metrics", {}) for row in self.db.content_items.find({"source_id": source_id}, {"metrics": 1})]

    def source_item_ids(self, source_id: str) -> list[int]:
        return [row["_id"] for row in self.db.content_items.find({"source_id": source_id}, {"_id": 1})]


store = MongoStore()
