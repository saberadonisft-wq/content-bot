from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Any

import certifi
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from .config import settings
from .mongo_item_query import (
    PROJECTION_FIELD,
    projection,
    select_items,
    unchanged_input,
)

INDEX_VERSION = 5
_ACQUISITION_COLLECTION = re.compile(r"^acquisition_[a-z][a-z0-9_]{0,63}$")


def _validate_acquisition_collection(collection: str) -> str:
    value = str(collection).strip()
    if not _ACQUISITION_COLLECTION.fullmatch(value):
        raise ValueError("Invalid acquisition collection")
    return value


def configure_mongodb_dns() -> None:
    """Use explicitly configured resolvers for MongoDB SRV discovery when requested."""
    servers = [
        item.strip() for item in settings.mongodb_dns_servers.split(",") if item.strip()
    ]
    if not servers:
        return
    try:
        import dns.resolver

        resolver = dns.resolver.get_default_resolver()
        resolver.nameservers = servers
        resolver.timeout = 1.0
        resolver.lifetime = 3.0
    except Exception as err:
        print(
            f"[MongoStore] Warning: MongoDB DNS override could not be applied ({err})."
        )


class DummyCollection:
    def find(self, *args: Any, **kwargs: Any) -> DummyCollection:
        return self

    def find_one(self, *args: Any, **kwargs: Any) -> dict | None:
        return None

    def find_one_and_update(self, *args: Any, **kwargs: Any) -> dict | None:
        return None

    def insert_one(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def update_one(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def update_many(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def delete_one(self, *args: Any, **kwargs: Any) -> Any:
        class Result:
            deleted_count = 0

        return Result()

    def delete_many(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def create_index(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def sort(self, *args: Any, **kwargs: Any) -> DummyCollection:
        return self

    def limit(self, *args: Any, **kwargs: Any) -> DummyCollection:
        return self

    def __iter__(self) -> Any:
        return iter([])


class DummyDatabase:
    def __getattr__(self, name: str) -> DummyCollection:
        return DummyCollection()

    def __getitem__(self, name: str) -> DummyCollection:
        return DummyCollection()


class MongoStore:
    """MongoDB persistence for the API, scheduler, and ingestion pipeline."""

    storage_name = "mongodb"

    def __init__(self, database: Database | None = None) -> None:
        self._ping_lock = Lock()
        self._last_ping_at = 0.0
        self._last_ping_result = False
        if database is not None:
            self.client = database.client
            self.db = database
            self._ping_enabled = False
            self._available = True
            return
        self.client = None
        self.db = DummyDatabase()
        self._ping_enabled = True
        self._available = False

    def connect(self) -> bool:
        """Connect on application startup instead of while Python imports modules."""
        if self.is_available:
            return True
        uri = settings.mongodb_uri or "mongodb://localhost:27017"
        kwargs: dict[str, Any] = {
            "tz_aware": True,
            "serverSelectionTimeoutMS": 2000,
            "connectTimeoutMS": 2000,
        }
        if (
            uri.startswith("mongodb+srv://")
            or "tls=true" in uri.lower()
            or "ssl=true" in uri.lower()
        ):
            configure_mongodb_dns()
            kwargs["tlsCAFile"] = certifi.where()

        try:
            client = MongoClient(uri, **kwargs)
            client.admin.command("ping")
            self.client = client
            self.db = self.client[settings.mongodb_database]
            target_name = (
                "Local MongoDB"
                if "localhost" in uri or "127.0.0.1" in uri
                else "MongoDB Atlas"
            )
            print(f"[MongoStore] Connected to {target_name} successfully.")
        except Exception as err:
            print(
                f"[MongoStore] Warning: MongoDB connection failed ({err}). Persistence is unavailable."
            )
            self.client = None
            self.db = DummyDatabase()
            self._available = False
        else:
            self._available = True
            self._last_ping_at = monotonic()
            self._last_ping_result = True
        return self.is_available

    @property
    def is_available(self) -> bool:
        return self._available and self.client is not None

    def ping(self) -> bool:
        if not self.is_available:
            return False
        try:
            self.client.admin.command("ping")
        except Exception as err:
            print(f"[MongoStore] Warning: MongoDB ping failed: {err}")
            result = False
        else:
            result = True
        with self._ping_lock:
            self._last_ping_at = monotonic()
            self._last_ping_result = result
        return result

    def ping_cached(self, ttl_seconds: float = 5.0) -> bool:
        with self._ping_lock:
            if monotonic() - self._last_ping_at < ttl_seconds:
                return self._last_ping_result
        return self.ping()

    def initialize(self, ping: bool = False) -> None:
        if not self.connect():
            return
        if ping and self._ping_enabled:
            try:
                self.client.admin.command("ping")
            except Exception as err:
                print(f"[MongoStore] Warning: MongoDB Atlas ping failed: {err}")
        try:
            metadata = self.db.app_metadata.find_one({"_id": "mongo-index-version"})
            if metadata and metadata.get("version") == INDEX_VERSION:
                return
            self.db.keywords.create_index("normalized_name", unique=True)
            self.db.keywords.create_index([("updated_at", DESCENDING)])
            self.db.keywords.create_index(
                [("enabled", ASCENDING), ("next_run_at", ASCENDING)]
            )
            self.db.content_items.create_index(
                [("source_id", ASCENDING), ("external_id", ASCENDING)], unique=True
            )
            self.db.content_items.create_index(
                [("source_id", ASCENDING), ("published_at", DESCENDING)]
            )
            self.db.content_items.create_index("last_seen_at")
            self.db.content_items.create_index(f"{PROJECTION_FIELD}.version")
            self.db.comments.create_index(
                [("source_id", ASCENDING), ("external_id", ASCENDING)],
                unique=True,
            )
            self.db.comments.create_index(
                [
                    ("source_id", ASCENDING),
                    ("content_external_id", ASCENDING),
                    ("published_at", ASCENDING),
                ]
            )
            self.db.comments.create_index(
                [
                    ("source_id", ASCENDING),
                    ("content_external_id", ASCENDING),
                    ("root_external_id", ASCENDING),
                    ("parent_external_id", ASCENDING),
                ]
            )
            self.db.comments.create_index("content_item_id")
            self.db.comments.create_index("last_seen_at")
            self.db.item_keyword_matches.create_index(
                [("content_item_id", ASCENDING), ("keyword_id", ASCENDING)], unique=True
            )
            self.db.item_keyword_matches.create_index(
                [("keyword_id", ASCENDING), ("trend_score", DESCENDING)]
            )
            self.db.item_keyword_matches.create_index(
                [("keyword_id", ASCENDING), ("session_id", ASCENDING)]
            )
            self.db.metric_snapshots.create_index(
                [("content_item_id", ASCENDING), ("captured_at", ASCENDING)],
                unique=True,
            )
            self.db.crawl_batches.create_index(
                [("keyword_id", ASCENDING), ("started_at", DESCENDING)]
            )
            self.db.crawl_batches.create_index("state")
            self.db.source_runs.create_index("batch_id")
            self.db.source_runs.create_index(
                [("batch_id", ASCENDING), ("channel_id", ASCENDING)]
            )
            self.db.acquisition_selections.create_index(
                "idempotency_key",
                unique=True,
                partialFilterExpression={"idempotency_key": {"$type": "string"}},
            )
            self.db.app_metadata.update_one(
                {"_id": "mongo-index-version"},
                {"$set": {"version": INDEX_VERSION}},
                upsert=True,
            )
        except Exception as err:
            print(f"[MongoStore] Warning: MongoDB index creation skipped: {err}")

    def next_id(self, collection: str) -> int:
        if not self.is_available:
            raise RuntimeError("MongoDB storage is not available")
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
        result.pop(PROJECTION_FIELD, None)
        return result

    def keyword(self, keyword_id: int) -> dict[str, Any] | None:
        return self.public(self.db.keywords.find_one({"_id": keyword_id}))

    def keywords(self) -> list[dict[str, Any]]:
        return [
            self.public(row)
            for row in self.db.keywords.find().sort("updated_at", DESCENDING)
        ]

    def keyword_name_exists(
        self, normalized_name: str, exclude_id: int | None = None
    ) -> bool:
        query: dict[str, Any] = {"normalized_name": normalized_name}
        if exclude_id is not None:
            query["_id"] = {"$ne": exclude_id}
        return self.db.keywords.find_one(query, {"_id": 1}) is not None

    def create_keyword(self, values: dict[str, Any]) -> dict[str, Any]:
        row = {"_id": self.next_id("keywords"), **values}
        self.db.keywords.insert_one(row)
        return self.public(row)

    def update_keyword(
        self, keyword_id: int, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = self.db.keywords.find_one_and_update(
            {"_id": keyword_id}, {"$set": values}, return_document=ReturnDocument.AFTER
        )
        return self.public(row)

    def next_session_number(self, keyword_id: int) -> int:
        row = self.db.keywords.find_one_and_update(
            {"_id": keyword_id},
            {"$inc": {"session_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if row is None:
            raise ValueError("Keyword not found")
        return int(row.get("session_count", 1))

    def update_channel_checkpoint(
        self,
        keyword_id: int,
        channel_id: str,
        checkpoint: dict[str, Any],
        *,
        scanned_at: Any,
        status: str,
        error: str | None = None,
    ) -> None:
        self.db.keywords.update_one(
            {"_id": keyword_id, "channels.id": channel_id},
            {
                "$set": {
                    "channels.$.checkpoint": checkpoint,
                    "channels.$.last_scanned_at": scanned_at,
                    "channels.$.last_status": status,
                    "channels.$.last_error": error,
                    "updated_at": scanned_at,
                }
            },
        )

    def update_source_checkpoint(
        self,
        keyword_id: int,
        source_id: str,
        operation: str,
        checkpoint: dict[str, Any],
        *,
        updated_at: Any,
    ) -> None:
        """Persist a durable operation checkpoint without replacing siblings."""
        if not source_id or any(character in source_id for character in (".", "$")):
            raise ValueError("Unsafe source checkpoint key")
        if not operation or any(character in operation for character in (".", "$")):
            raise ValueError("Unsafe checkpoint operation key")
        self.db.keywords.update_one(
            {"_id": keyword_id},
            {
                "$set": {
                    f"source_checkpoints.{source_id}.{operation}": deepcopy(checkpoint),
                    "updated_at": updated_at,
                }
            },
        )

    def delete_keyword(self, keyword_id: int) -> bool:
        if self.db.keywords.delete_one({"_id": keyword_id}).deleted_count == 0:
            return False
        batch_ids = [
            row["_id"]
            for row in self.db.crawl_batches.find(
                {"keyword_id": keyword_id}, {"_id": 1}
            )
        ]
        if batch_ids:
            self.db.source_runs.delete_many({"batch_id": {"$in": batch_ids}})
            self.db.crawl_batches.delete_many({"_id": {"$in": batch_ids}})
        content_ids = list(
            {
                row["content_item_id"]
                for row in self.db.item_keyword_matches.find(
                    {"keyword_id": keyword_id}, {"content_item_id": 1}
                )
            }
        )
        self.db.item_keyword_matches.delete_many({"keyword_id": keyword_id})
        if content_ids:
            still_matched = {
                row["content_item_id"]
                for row in self.db.item_keyword_matches.find(
                    {"content_item_id": {"$in": content_ids}},
                    {"content_item_id": 1},
                )
            }
            orphan_ids = [cid for cid in content_ids if cid not in still_matched]
            for content_item_id in orphan_ids:
                self.delete_item(content_item_id)
        return True

    def delete_source_data(self, source_id: str) -> dict[str, int]:
        """Delete persisted crawler observations for one canonical source.

        Topic/channel configuration is preserved, but provider checkpoints and
        scan status are reset so a later re-enable cannot resume from erased
        state.
        """
        if not source_id or any(character in source_id for character in (".", "$")):
            raise ValueError("Unsafe source ID")
        content_ids = [
            row["_id"]
            for row in self.db.content_items.find({"source_id": source_id}, {"_id": 1})
        ]
        matches = snapshots = items = comments = 0
        if content_ids:
            matches = self.db.item_keyword_matches.delete_many(
                {"content_item_id": {"$in": content_ids}}
            ).deleted_count
            snapshots = self.db.metric_snapshots.delete_many(
                {"content_item_id": {"$in": content_ids}}
            ).deleted_count
            items = self.db.content_items.delete_many(
                {"_id": {"$in": content_ids}}
            ).deleted_count
        comments = self.db.comments.delete_many({"source_id": source_id}).deleted_count
        source_runs = self.db.source_runs.delete_many(
            {"source_id": source_id}
        ).deleted_count
        keywords_reset = 0
        for keyword in self.db.keywords.find(
            {
                "$or": [
                    {f"source_checkpoints.{source_id}": {"$exists": True}},
                    {"channels.source_id": source_id},
                ]
            }
        ):
            checkpoints = dict(keyword.get("source_checkpoints") or {})
            checkpoints.pop(source_id, None)
            channels = []
            for channel in keyword.get("channels", []):
                if channel.get("source_id") == source_id:
                    channel = {
                        **channel,
                        "checkpoint": {},
                        "last_scanned_at": None,
                        "last_status": None,
                        "last_error": None,
                    }
                channels.append(channel)
            self.db.keywords.update_one(
                {"_id": keyword["_id"]},
                {
                    "$set": {
                        "source_checkpoints": checkpoints,
                        "channels": channels,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            keywords_reset += 1
        return {
            "content_items": int(items),
            "comments": int(comments),
            "item_matches": int(matches),
            "metric_snapshots": int(snapshots),
            "source_runs": int(source_runs),
            "keywords_reset": keywords_reset,
        }

    def create_batch(
        self, batch: dict[str, Any], source_runs: list[dict[str, Any]]
    ) -> None:
        self.db.crawl_batches.insert_one({"_id": batch.pop("id"), **batch})
        if source_runs:
            self.db.source_runs.insert_many(
                [{"_id": row.pop("id"), **row} for row in source_runs]
            )

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

    def batches(
        self, keyword_id: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        query = {} if keyword_id is None else {"keyword_id": keyword_id}
        rows = (
            self.db.crawl_batches.find(query)
            .sort("started_at", DESCENDING)
            .limit(limit)
        )
        result = []
        for raw in rows:
            row = self.public(raw)
            row["source_runs"] = self.source_runs(row["id"])
            result.append(row)
        return result

    def update_batch(self, batch_id: str, values: dict[str, Any]) -> None:
        self.db.crawl_batches.update_one({"_id": batch_id}, {"$set": values})

    def prune_sessions(self, keyword_id: int, keep: int = 5) -> list[str]:
        completed = list(
            self.db.crawl_batches.find(
                {"keyword_id": keyword_id, "state": {"$nin": ["queued", "running"]}}
            ).sort([("finished_at", DESCENDING), ("started_at", DESCENDING)])
        )
        expired_ids = [row["_id"] for row in completed[keep:]]
        if not expired_ids:
            return []
        content_ids = list(
            {
                row["content_item_id"]
                for row in self.db.item_keyword_matches.find(
                    {"keyword_id": keyword_id, "session_id": {"$in": expired_ids}},
                    {"content_item_id": 1},
                )
            }
        )
        self.db.item_keyword_matches.delete_many(
            {"keyword_id": keyword_id, "session_id": {"$in": expired_ids}}
        )
        self.db.source_runs.delete_many({"batch_id": {"$in": expired_ids}})
        self.db.crawl_batches.delete_many({"_id": {"$in": expired_ids}})
        for content_item_id in content_ids:
            if (
                self.db.item_keyword_matches.find_one(
                    {"content_item_id": content_item_id}, {"_id": 1}
                )
                is None
            ):
                self.delete_item(content_item_id)
        return expired_ids

    def source_run(self, source_run_id: str) -> dict[str, Any] | None:
        return self.public(self.db.source_runs.find_one({"_id": source_run_id}))

    def source_runs(self, batch_id: str) -> list[dict[str, Any]]:
        return [
            self.public(row) for row in self.db.source_runs.find({"batch_id": batch_id})
        ]

    def update_source_run(
        self,
        source_run_id: str,
        values: dict[str, Any],
        increments: dict[str, int] | None = None,
    ) -> None:
        update: dict[str, Any] = {}
        if values:
            update["$set"] = values
        if increments:
            update["$inc"] = increments
        if not update:
            return
        self.db.source_runs.update_one({"_id": source_run_id}, update)

    def item_by_source(self, source_id: str, external_id: str) -> dict[str, Any] | None:
        return self.public(
            self.db.content_items.find_one(
                {"source_id": source_id, "external_id": external_id}
            )
        )

    def item(self, content_item_id: int) -> dict[str, Any] | None:
        return self.public(self.db.content_items.find_one({"_id": content_item_id}))

    def save_item(self, values: dict[str, Any]) -> dict[str, Any]:
        values = deepcopy(values)
        content_item_id = values.pop("id", None)
        if content_item_id is None:
            content_item_id = self.next_id("content_items")
        values.pop(PROJECTION_FIELD, None)
        for _ in range(16):
            previous = self.db.content_items.find_one({"_id": content_item_id})
            document = {**(previous or {}), **values, "_id": content_item_id}
            materialized = projection(document)
            if previous is None:
                self.db.content_items.insert_one({**document, PROJECTION_FIELD: materialized})
                return {"id": content_item_id, **values}
            result = self.db.content_items.update_one(
                unchanged_input(previous), {"$set": {**values, PROJECTION_FIELD: materialized}},
            )
            if result.matched_count:
                return {"id": content_item_id, **values}
        raise RuntimeError("Content changed repeatedly while saving item; retry ingestion")

    def query_items(self, query, *, limit=None, offset=0):
        return select_items(self, query, limit, offset)

    def ingest_content_bundle(
        self,
        item_values: dict[str, Any],
        snapshot_values: dict[str, Any],
        match_values: dict[str, Any],
        keyword_id: int,
        trend_score_fn: Callable[[dict[str, Any]], float] | None = None,
    ) -> dict[str, Any]:
        """Persist a bundle in order; standalone MongoDB has no multi-document transaction."""
        item = self.save_item(item_values)
        snapshot = deepcopy(snapshot_values)
        snapshot["content_item_id"] = item["id"]
        self.add_snapshot(snapshot)
        match_data = deepcopy(match_values)
        if trend_score_fn is not None and "trend_score" not in match_data:
            match_data["trend_score"] = trend_score_fn(item)
        self.save_match(item["id"], keyword_id, match_data)
        return item

    def comment_by_source(
        self, source_id: str, external_id: str
    ) -> dict[str, Any] | None:
        return self.public(
            self.db.comments.find_one(
                {"source_id": source_id, "external_id": external_id}
            )
        )

    def save_comment(self, values: dict[str, Any]) -> dict[str, Any]:
        """Idempotently persist one normalized crawler comment.

        The provider identity is `(source_id, external_id)`.  A comment is also
        anchored to the canonical parent content identity so deletion and
        retention can cascade without retaining provider account data.
        """
        values = deepcopy(values)
        source_id = str(values.get("source_id") or "").strip()
        external_id = str(values.get("external_id") or "").strip()
        content_external_id = str(values.get("content_external_id") or "").strip()
        if not source_id or not external_id or not content_external_id:
            raise ValueError("Comment identity fields cannot be empty")
        if any(character in source_id for character in (".", "$")):
            raise ValueError("Unsafe comment source ID")
        if max(len(external_id), len(content_external_id)) > 256:
            raise ValueError("Comment identity is too long")
        parent = values.get("parent_external_id")
        if parent is not None and str(parent) == external_id:
            raise ValueError("Comment cannot be its own parent")
        content = self.db.content_items.find_one(
            {
                "source_id": source_id,
                "external_id": content_external_id,
            },
            {"_id": 1},
        )
        if content is None:
            raise ValueError("Comment parent content does not exist")
        supplied_content_item_id = values.get("content_item_id")
        if (
            supplied_content_item_id is not None
            and supplied_content_item_id != content["_id"]
        ):
            raise ValueError("Comment parent content identity is inconsistent")
        body = str(values.get("body") or "")[:4_000]
        author = str(values.get("author_pseudonym") or "")[:128]
        values.update(
            {
                "source_id": source_id,
                "external_id": external_id,
                "content_external_id": content_external_id,
                "content_item_id": content["_id"],
                "body": body,
                "author_pseudonym": author,
            }
        )
        now = values.get("last_seen_at") or datetime.now(UTC)
        values["last_seen_at"] = now
        first_seen_at = values.pop("first_seen_at", None) or now
        row = self.db.comments.find_one_and_update(
            {"source_id": source_id, "external_id": external_id},
            {
                "$set": values,
                "$setOnInsert": {
                    "_id": self.next_id("comments"),
                    "first_seen_at": first_seen_at,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return self.public(row)

    def comments_for_content(
        self,
        source_id: str,
        content_external_id: str,
        *,
        limit: int = 200,
        root_external_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Comment list limit must be between 1 and 1000")
        query: dict[str, Any] = {
            "source_id": source_id,
            "content_external_id": content_external_id,
        }
        if root_external_id is not None:
            query["root_external_id"] = root_external_id
        rows = (
            self.db.comments.find(query)
            .sort([("published_at", ASCENDING), ("_id", ASCENDING)])
            .limit(limit)
        )
        return [self.public(row) for row in rows]

    def delete_comments_for_content(
        self, source_id: str, content_external_id: str
    ) -> int:
        result = self.db.comments.delete_many(
            {
                "source_id": source_id,
                "content_external_id": content_external_id,
            }
        )
        return int(result.deleted_count)

    def delete_item(self, content_item_id: int) -> None:
        item = self.db.content_items.find_one(
            {"_id": content_item_id},
            {"source_id": 1, "external_id": 1},
        )
        if item:
            self.delete_comments_for_content(
                str(item.get("source_id") or ""),
                str(item.get("external_id") or ""),
            )
        self.db.content_items.delete_one({"_id": content_item_id})
        self.db.item_keyword_matches.delete_many({"content_item_id": content_item_id})
        self.db.metric_snapshots.delete_many({"content_item_id": content_item_id})

    def match(self, content_item_id: int, keyword_id: int) -> dict[str, Any] | None:
        return self.public(
            self.db.item_keyword_matches.find_one(
                {"content_item_id": content_item_id, "keyword_id": keyword_id}
            )
        )

    def has_external_match(
        self, source_id: str, external_id: str, keyword_id: int
    ) -> bool:
        item = self.db.content_items.find_one(
            {"source_id": source_id, "external_id": external_id}, {"_id": 1}
        )
        return bool(
            item
            and self.db.item_keyword_matches.find_one(
                {"content_item_id": item["_id"], "keyword_id": keyword_id}, {"_id": 1}
            )
        )

    def save_match(
        self, content_item_id: int, keyword_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        row = self.db.item_keyword_matches.find_one_and_update(
            {"content_item_id": content_item_id, "keyword_id": keyword_id},
            {
                "$set": values,
                "$setOnInsert": {"_id": self.next_id("item_keyword_matches")},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return self.public(row)

    def delete_match(self, content_item_id: int, keyword_id: int) -> None:
        self.db.item_keyword_matches.delete_one(
            {"content_item_id": content_item_id, "keyword_id": keyword_id}
        )
        if (
            self.db.item_keyword_matches.find_one(
                {"content_item_id": content_item_id}, {"_id": 1}
            )
            is None
        ):
            self.delete_item(content_item_id)

    def add_snapshot(self, values: dict[str, Any]) -> None:
        self.db.metric_snapshots.update_one(
            {
                "content_item_id": values["content_item_id"],
                "captured_at": values["captured_at"],
            },
            {"$set": values},
            upsert=True,
        )

    def snapshots(
        self, content_item_id: int, descending: bool = False, limit: int = 0
    ) -> list[dict[str, Any]]:
        direction = DESCENDING if descending else ASCENDING
        cursor = self.db.metric_snapshots.find(
            {"content_item_id": content_item_id}
        ).sort("captured_at", direction)
        if limit:
            cursor = cursor.limit(limit)
        return [self.public(row) for row in cursor]

    def item_matches(
        self,
        keyword_id: int,
        positive_only: bool = False,
        source_id: str | None = None,
        min_relevance: float = 0,
        session_id: str | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        query: dict[str, Any] = {"keyword_id": keyword_id}
        if session_id:
            query["session_id"] = session_id
        if min_relevance > 0:
            query["relevance_score"] = {"$gte": min_relevance}
        elif positive_only:
            query["relevance_score"] = {"$gt": 0}
        raw_matches = list(self.db.item_keyword_matches.find(query))
        item_ids = [row["content_item_id"] for row in raw_matches]
        item_query: dict[str, Any] = {"_id": {"$in": item_ids}}
        if source_id:
            item_query["source_id"] = source_id
        items = {
            row["_id"]: self.public(row)
            for row in self.db.content_items.find(item_query)
        }
        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for raw_match in raw_matches:
            item = items.get(raw_match["content_item_id"])
            if item is not None:
                rows.append((item, self.public(raw_match)))
        return rows

    def source_metric_values(self, source_id: str) -> list[dict[str, int]]:
        return [
            row.get("metrics", {})
            for row in self.db.content_items.find(
                {"source_id": source_id}, {"metrics": 1}
            )
        ]

    def source_item_ids(self, source_id: str) -> list[int]:
        return [
            row["_id"]
            for row in self.db.content_items.find({"source_id": source_id}, {"_id": 1})
        ]

    def source_snapshot_pairs(self, source_id: str) -> list[list[dict[str, Any]]]:
        from .services.snapshot_pairs import latest_pairs

        rows = self.db.metric_snapshots.aggregate([
            {"$lookup": {"from": "content_items", "localField": "content_item_id", "foreignField": "_id", "as": "item"}},
            {"$match": {"item.source_id": source_id}},
            {"$project": {"item": 0}},
        ])
        return latest_pairs(self.public(row) for row in rows)

    def metadata(self, key: str) -> dict[str, Any] | None:
        return self.public(self.db.app_metadata.find_one({"_id": key}))

    def set_metadata(self, key: str, values: dict[str, Any]) -> None:
        self.db.app_metadata.update_one({"_id": key}, {"$set": values}, upsert=True)

    def acquisition_documents(
        self, collection: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        collection = _validate_acquisition_collection(collection)
        if limit is not None and (limit < 1 or limit > 100_000):
            raise ValueError("Invalid acquisition document limit")
        cursor = self.db[collection].find().sort("updated_at", DESCENDING)
        if limit is not None:
            cursor = cursor.limit(limit)
        return [self.public(row) for row in cursor]

    def acquisition_document(
        self, collection: str, document_id: str
    ) -> dict[str, Any] | None:
        collection = _validate_acquisition_collection(collection)
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("Invalid acquisition document ID")
        return self.public(self.db[collection].find_one({"_id": document_id}))

    def upsert_acquisition_document(
        self, collection: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        collection = _validate_acquisition_collection(collection)
        document = deepcopy(values)
        identifier = document.get("_id", document.get("id"))
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256:
            raise ValueError("Acquisition document ID is required")
        document["_id"] = identifier
        document.pop("id", None)
        self.db[collection].replace_one({"_id": identifier}, document, upsert=True)
        return self.public(document)

    def reserve_acquisition_selection(
        self, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Atomically reserve a selection idempotency key when present."""
        document = deepcopy(values)
        identifier = document.get("_id", document.get("id"))
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256:
            raise ValueError("Acquisition selection ID is required")
        document["_id"] = identifier
        document.pop("id", None)
        key = document.get("idempotency_key")
        if key is None:
            self.db["acquisition_selections"].replace_one(
                {"_id": identifier}, document, upsert=True
            )
            return self.public(document)
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise ValueError("Acquisition selection idempotency key is invalid")
        try:
            row = self.db["acquisition_selections"].find_one_and_update(
                {"idempotency_key": key},
                {"$setOnInsert": document},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            row = self.db["acquisition_selections"].find_one(
                {"idempotency_key": key}
            )
        return self.public(row)

    def delete_acquisition_document(self, collection: str, document_id: str) -> bool:
        collection = _validate_acquisition_collection(collection)
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("Invalid acquisition document ID")
        return self.db[collection].delete_one({"_id": document_id}).deleted_count > 0

    def advance_acquisition_pagination(
        self, run_id: str, *, expected_generation: int, now: str
    ) -> dict[str, Any] | None:
        return self.public(self.db["acquisition_runs"].find_one_and_update(
            {"_id": run_id, "state": "completed", "pagination.generation": expected_generation,
             "pagination.exhausted": False, "pagination.next_offset": {"$lt": 5000}},
            {"$inc": {"pagination.generation": 1}, "$set": {
                "state": "queued", "phase": "queued", "error": None,
                "error_code": None, "stop_reason": None, "updated_at": now,
            }}, return_document=ReturnDocument.AFTER,
        ))

    def claim_acquisition_channel(
        self,
        channel_id: str,
        *,
        run_id: str,
        now: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("Invalid acquisition channel ID")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("Invalid acquisition run ID")
        row = self.db["acquisition_channels"].find_one_and_update(
            {
                "_id": channel_id,
                "subscription.enabled": True,
                "$or": [
                    {"subscription.active_run_id": {"$exists": False}},
                    {"subscription.active_run_id": None},
                    {"subscription.lease_expires_at": {"$lte": now}},
                ],
                "$and": [
                    {
                        "$or": [
                            {"subscription.next_run_at": {"$exists": False}},
                            {"subscription.next_run_at": None},
                            {"subscription.next_run_at": {"$lte": now}},
                        ]
                    }
                ],
            },
            {
                "$set": {
                    "subscription.active_run_id": run_id,
                    "subscription.lease_expires_at": lease_expires_at,
                    "subscription.last_status": "queued",
                    "subscription.last_error": None,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return self.public(row)

    def claim_acquisition_reconciliation(
        self,
        channel_id: str,
        *,
        run_id: str,
        now: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        """Atomically claim one terminal run without requiring a Mongo transaction."""
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("Invalid acquisition channel ID")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("Invalid acquisition run ID")
        row = self.db["acquisition_channels"].find_one_and_update(
            {
                "_id": channel_id,
                "subscription.active_run_id": run_id,
                "$or": [
                    {"subscription.reconcile_run_id": {"$exists": False}},
                    {"subscription.reconcile_run_id": None},
                    {"subscription.reconcile_lease_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "subscription.reconcile_run_id": run_id,
                    "subscription.reconcile_lease_token": uuid.uuid4().hex,
                    "subscription.reconcile_lease_expires_at": lease_expires_at,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return self.public(row)

    def complete_acquisition_reconciliation(
        self,
        channel_id: str,
        *,
        run_id: str,
        lease_token: str,
        now: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Commit finalizer state only while this process owns its lease."""
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("Reconciliation lease token is required")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("Invalid acquisition channel ID")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("Invalid acquisition run ID")
        set_values = {f"subscription.{key}": deepcopy(value) for key, value in updates.items()}
        set_values["updated_at"] = now
        row = self.db["acquisition_channels"].find_one_and_update(
            {
                "_id": channel_id,
                "subscription.active_run_id": run_id,
                "subscription.reconcile_run_id": run_id,
                "subscription.reconcile_lease_token": lease_token,
                "subscription.reconcile_lease_expires_at": {"$gt": now},
            },
            {"$set": set_values},
            return_document=ReturnDocument.AFTER,
        )
        return self.public(row)

    def due_keywords(self, now: datetime) -> list[dict[str, Any]]:
        return [
            self.public(row)
            for row in self.db.keywords.find(
                {"enabled": True, "next_run_at": {"$ne": None, "$lte": now}}
            )
        ]

    def content_item_ids_before(self, cutoff: datetime) -> list[int]:
        return [
            row["_id"]
            for row in self.db.content_items.find(
                {"last_seen_at": {"$lt": cutoff}}, {"_id": 1}
            )
        ]

    def batches_by_states(self, states: set[str]) -> list[dict[str, Any]]:
        return [
            self.public(row)
            for row in self.db.crawl_batches.find({"state": {"$in": list(states)}})
        ]

    def delete_irrelevant_matches(self) -> tuple[int, set[int]]:
        matches = list(
            self.db.item_keyword_matches.find({"relevance_score": {"$lte": 0}})
        )
        candidates = {row["content_item_id"] for row in matches}
        if matches:
            self.db.item_keyword_matches.delete_many(
                {"_id": {"$in": [row["_id"] for row in matches]}}
            )
        return len(matches), candidates

    def has_matches_for_item(self, content_item_id: int) -> bool:
        return (
            self.db.item_keyword_matches.find_one(
                {"content_item_id": content_item_id}, {"_id": 1}
            )
            is not None
        )

    def recent_items(
        self, source_ids: set[str], limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = (
            self.db.content_items.find({"source_id": {"$in": list(source_ids)}})
            .sort("published_at", DESCENDING)
            .limit(limit)
        )
        return [self.public(row) for row in rows]


from .sqlite_store import SQLiteStore
from .storage_protocol import PersistenceStore


def create_store() -> PersistenceStore:
    storage_backend = settings.content_bot_storage_backend.strip().lower()
    if storage_backend == "mongodb":
        return MongoStore()
    if storage_backend == "sqlite":
        return SQLiteStore(settings.sqlite_path)
    raise RuntimeError("CONTENT_BOT_STORAGE_BACKEND must be either 'sqlite' or 'mongodb'")


# Compatibility for standalone ingestion scripts; construction does not connect.
store: PersistenceStore = create_store()
