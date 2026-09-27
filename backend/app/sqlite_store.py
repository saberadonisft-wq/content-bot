from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import RLock, local
from typing import Any

from .sqlite_indexes import ensure_query_indexes
from .sqlite_item_query import (
    initialize_item_query,
    queue_existing_analysis,
    select_items,
    update_item_analysis,
)

COLLECTIONS = (
    "keywords",
    "crawl_batches",
    "source_runs",
    "content_items",
    "comments",
    "item_keyword_matches",
    "metric_snapshots",
)

LEGACY_JSON_FIELDS = {
    "keywords": {
        "include_terms_json": "include_terms",
        "exclude_terms_json": "exclude_terms",
        "source_ids_json": "source_ids",
    },
    "source_runs": {"checkpoint_json": "checkpoint"},
    "content_items": {
        "hashtags_json": "hashtags",
        "metrics_json": "metrics",
        "raw_payload_json": "raw_payload",
    },
    "item_keyword_matches": {"match_reasons_json": "match_reasons"},
}

LEGACY_DATE_FIELDS = {
    "keywords": {"next_run_at", "created_at", "updated_at"},
    "crawl_batches": {"started_at", "finished_at"},
    "source_runs": {"started_at", "finished_at"},
    "content_items": {"published_at", "first_seen_at", "last_seen_at"},
    "item_keyword_matches": {"updated_at"},
    "metric_snapshots": {"captured_at"},
}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__content_bot_datetime__": value.isoformat()}
    raise TypeError(f"Unsupported SQLite document value: {type(value).__name__}")


def _json_hook(value: dict[str, Any]) -> Any:
    encoded = value.get("__content_bot_datetime__")
    if len(value) == 1 and isinstance(encoded, str):
        parsed = datetime.fromisoformat(encoded)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return value


def _sort_value(value: Any) -> tuple[bool, Any]:
    return value is None, value


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^\w#]+", " ", value).strip()


_ACQUISITION_COLLECTION = re.compile(r"^acquisition_[a-z][a-z0-9_]{0,63}$")


def _validate_acquisition_collection(collection: str) -> str:
    value = str(collection).strip()
    if not _ACQUISITION_COLLECTION.fullmatch(value):
        raise ValueError("Invalid acquisition collection")
    return value


def _transactional(method: Callable) -> Callable:
    """Keep each complete mutation, including nested reads, in one transaction."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._transaction():
            return method(self, *args, **kwargs)
    return wrapped


class SQLiteStore:
    """Local document persistence backed by one durable SQLite database."""

    storage_name = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._write_lock = RLock()
        self._local = local()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
        else:
            with closing(self._connect()) as connection, connection:
                yield connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
            return
        with self._write_lock, closing(self._connect()) as connection:
            self._local.connection = connection
            try:
                with connection:
                    # Reserve the writer before reading. SQLite also serializes
                    # other Store instances/processes; an instance RLock cannot.
                    connection.execute("BEGIN IMMEDIATE")
                    yield connection
            finally:
                del self._local.connection

    @property
    def is_available(self) -> bool:
        return self.ping_cached()

    def ping(self) -> bool:
        try:
            with self._connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def ping_cached(self, ttl_seconds: float = 5.0) -> bool:
        del ttl_seconds
        return self.ping()

    def initialize(self, ping: bool = False) -> None:
        del ping
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_documents (
                    collection TEXT NOT NULL,
                    document_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (collection, document_key)
                );
                CREATE INDEX IF NOT EXISTS ix_local_documents_collection
                    ON local_documents (collection);
                CREATE TABLE IF NOT EXISTS local_counters (
                    collection TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_metadata (
                    metadata_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                """
            )
            initialize_item_query(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._import_legacy_tables(connection)
            self._upgrade_local_documents(connection)
            ensure_query_indexes(connection)
            queue_existing_analysis(connection)

    def _upgrade_local_documents(self, connection: sqlite3.Connection) -> None:
        marker = connection.execute(
            "SELECT 1 FROM local_metadata WHERE metadata_key = ?",
            ("local-document-schema-v2",),
        ).fetchone()
        if marker:
            return
        rows = connection.execute(
            "SELECT document_key, payload FROM local_documents WHERE collection = ?",
            ("keywords",),
        ).fetchall()
        for raw in rows:
            document = self._loads(raw["payload"])
            document.setdefault(
                "normalized_name", _normalized(document.get("name", ""))
            )
            document.setdefault("channels", [])
            document.setdefault("source_checkpoints", {})
            document.setdefault("source_selection_version", 1)
            document.setdefault("session_count", 0)
            self._upsert_with(connection, "keywords", document["_id"], document)
        connection.execute(
            "INSERT INTO local_metadata(metadata_key, payload) VALUES (?, ?)",
            (
                "local-document-schema-v2",
                self._dumps({"completed_at": datetime.now(UTC)}),
            ),
        )

    def _import_legacy_tables(self, connection: sqlite3.Connection) -> None:
        marker = connection.execute(
            "SELECT 1 FROM local_metadata WHERE metadata_key = ?",
            ("legacy-sqlite-import-v1",),
        ).fetchone()
        if marker:
            return
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for collection in COLLECTIONS:
            if collection not in table_names or collection == "comments":
                continue
            rows = connection.execute(f'SELECT * FROM "{collection}"').fetchall()
            max_numeric_id = 0
            for raw in rows:
                document = dict(raw)
                for old_name, new_name in LEGACY_JSON_FIELDS.get(
                    collection, {}
                ).items():
                    encoded = document.pop(old_name, None)
                    try:
                        document[new_name] = (
                            json.loads(encoded)
                            if encoded
                            else ([] if new_name != "checkpoint" else {})
                        )
                    except (TypeError, json.JSONDecodeError):
                        document[new_name] = [] if new_name != "checkpoint" else {}
                for field in LEGACY_DATE_FIELDS.get(collection, set()):
                    encoded = document.get(field)
                    if isinstance(encoded, str) and encoded:
                        try:
                            parsed = datetime.fromisoformat(encoded)
                            document[field] = (
                                parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                            )
                        except ValueError:
                            pass
                identifier = document.pop("id")
                document["_id"] = identifier
                if isinstance(identifier, int):
                    max_numeric_id = max(max_numeric_id, identifier)
                self._upsert_with(connection, collection, identifier, document)
            if max_numeric_id:
                connection.execute(
                    "INSERT INTO local_counters(collection, value) VALUES (?, ?) "
                    "ON CONFLICT(collection) DO UPDATE SET value = MAX(value, excluded.value)",
                    (collection, max_numeric_id),
                )
        connection.execute(
            "INSERT INTO local_metadata(metadata_key, payload) VALUES (?, ?)",
            (
                "legacy-sqlite-import-v1",
                self._dumps({"completed_at": datetime.now(UTC)}),
            ),
        )

    @staticmethod
    def _key(identifier: Any) -> str:
        prefix = "i" if isinstance(identifier, int) else "s"
        return f"{prefix}:{identifier}"

    @staticmethod
    def _dumps(document: Any) -> str:
        return json.dumps(
            document, default=_json_default, ensure_ascii=False, separators=(",", ":")
        )

    @staticmethod
    def _loads(payload: str) -> Any:
        return json.loads(payload, object_hook=_json_hook)

    def _upsert_with(
        self,
        connection: sqlite3.Connection,
        collection: str,
        identifier: Any,
        document: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO local_documents(collection, document_key, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(collection, document_key) DO UPDATE SET payload = excluded.payload",
            (collection, self._key(identifier), self._dumps(document)),
        )
        if collection == "content_items":
            update_item_analysis(connection, self._key(identifier), document)

    @_transactional
    def _upsert(self, collection: str, document: dict[str, Any]) -> None:
        with self._write_lock, self._connection() as connection:
            self._upsert_with(connection, collection, document["_id"], document)

    def _all(self, collection: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM local_documents WHERE collection = ?",
                (collection,),
            ).fetchall()
        return [self._loads(row["payload"]) for row in rows]

    def _get(self, collection: str, identifier: Any) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM local_documents WHERE collection = ? AND document_key = ?",
                (collection, self._key(identifier)),
            ).fetchone()
        return self._loads(row["payload"]) if row else None

    def acquisition_documents(
        self, collection: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        collection = _validate_acquisition_collection(collection)
        if limit is not None and (limit < 1 or limit > 100_000):
            raise ValueError("Invalid acquisition document limit")
        with self._connection() as connection:
            sql = (
                "SELECT payload FROM local_documents "
                "WHERE collection = ? ORDER BY rowid DESC"
            )
            values: tuple[Any, ...] = (collection,)
            if limit is not None:
                sql += " LIMIT ?"
                values = (collection, limit)
            rows = connection.execute(sql, values).fetchall()
        return [self.public(self._loads(row["payload"])) for row in rows]

    def acquisition_document(
        self, collection: str, document_id: str
    ) -> dict[str, Any] | None:
        collection = _validate_acquisition_collection(collection)
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("Invalid acquisition document ID")
        return self.public(self._get(collection, document_id))

    @_transactional
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
        with self._write_lock, self._connection() as connection:
            self._upsert_with(connection, collection, identifier, document)
        return self.public(document)

    @_transactional
    def reserve_acquisition_selection(
        self, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert one selection, returning the winner for an idempotency key."""
        document = deepcopy(values)
        identifier = document.get("_id", document.get("id"))
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256:
            raise ValueError("Acquisition selection ID is required")
        document["_id"] = identifier
        document.pop("id", None)
        key = document.get("idempotency_key")
        if key is not None:
            if not isinstance(key, str) or not key.strip() or len(key) > 256:
                raise ValueError("Acquisition selection idempotency key is invalid")
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT payload FROM local_documents "
                    "WHERE collection = ?",
                    ("acquisition_selections",),
                ).fetchall()
            for row in rows:
                existing = self._loads(row["payload"])
                if existing.get("idempotency_key") == key:
                    return self.public(existing)
        with self._write_lock, self._connection() as connection:
            self._upsert_with(
                connection, "acquisition_selections", identifier, document
            )
        return self.public(document)

    @_transactional
    def delete_acquisition_document(self, collection: str, document_id: str) -> bool:
        collection = _validate_acquisition_collection(collection)
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("Invalid acquisition document ID")
        with self._write_lock, self._connection() as connection:
            result = connection.execute(
                "DELETE FROM local_documents WHERE collection = ? AND document_key = ?",
                (collection, self._key(document_id)),
            )
        return result.rowcount > 0

    @_transactional
    def advance_acquisition_pagination(
        self, run_id: str, *, expected_generation: int, now: str
    ) -> dict[str, Any] | None:
        run = self._get("acquisition_runs", run_id)
        pagination = (run or {}).get("pagination") or {}
        if (not run or run.get("state") != "completed"
                or pagination.get("generation") != expected_generation
                or pagination.get("exhausted") is not False
                or int(pagination.get("next_offset", 5000)) >= 5000):
            return None
        pagination["generation"] = expected_generation + 1
        run.update(state="queued", phase="queued", error=None, error_code=None,
                   stop_reason=None, updated_at=now)
        with self._connection() as connection:
            self._upsert_with(connection, "acquisition_runs", run_id, run)
        return self.public(run)

    @_transactional
    def claim_acquisition_channel(
        self,
        channel_id: str,
        *,
        run_id: str,
        now: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        channel = self._get("acquisition_channels", channel_id)
        if channel is None:
            return None
        subscription = dict(channel.get("subscription") or {})
        if not subscription.get("enabled"):
            return None
        active_run_id = subscription.get("active_run_id")
        active_lease = subscription.get("lease_expires_at")
        if active_run_id and not (
            isinstance(active_lease, str) and active_lease <= now
        ):
            return None
        next_run_at = subscription.get("next_run_at")
        if isinstance(next_run_at, str) and next_run_at > now:
            return None
        subscription.update(
            active_run_id=run_id,
            lease_expires_at=lease_expires_at,
            last_status="queued",
            last_error=None,
        )
        channel["subscription"] = subscription
        channel["updated_at"] = now
        with self._write_lock, self._connection() as connection:
            self._upsert_with(connection, "acquisition_channels", channel_id, channel)
        return self.public(channel)

    @_transactional
    def claim_acquisition_reconciliation(
        self,
        channel_id: str,
        *,
        run_id: str,
        now: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        """Claim one terminal subscription run for durable reconciliation.

        The read and write deliberately stay inside ``BEGIN IMMEDIATE``. The
        scheduler lease protects run creation, while this second lease protects
        the multi-document finalization/replay step after a run is complete.
        """
        channel = self._get("acquisition_channels", channel_id)
        if channel is None:
            return None
        subscription = dict(channel.get("subscription") or {})
        if subscription.get("active_run_id") != run_id:
            return None
        active_lease = subscription.get("reconcile_lease_expires_at")
        reconcile_run_id = subscription.get("reconcile_run_id")
        if reconcile_run_id and not (
            isinstance(active_lease, str) and active_lease <= now
        ):
            return None
        subscription.update(
            reconcile_run_id=run_id,
            reconcile_lease_token=uuid.uuid4().hex,
            reconcile_lease_expires_at=lease_expires_at,
        )
        channel["subscription"] = subscription
        channel["updated_at"] = now
        with self._write_lock, self._connection() as connection:
            self._upsert_with(connection, "acquisition_channels", channel_id, channel)
        return self.public(channel)

    @_transactional
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
        channel = self._get("acquisition_channels", channel_id)
        if channel is None:
            return None
        subscription = dict(channel.get("subscription") or {})
        if (
            subscription.get("active_run_id") != run_id
            or subscription.get("reconcile_run_id") != run_id
            or subscription.get("reconcile_lease_token") != lease_token
            or not isinstance(subscription.get("reconcile_lease_expires_at"), str)
            or subscription["reconcile_lease_expires_at"] <= now
        ):
            return None
        subscription.update(deepcopy(updates))
        channel["subscription"] = subscription
        channel["updated_at"] = now
        with self._write_lock, self._connection() as connection:
            self._upsert_with(connection, "acquisition_channels", channel_id, channel)
        return self.public(channel)

    def _find(
        self, collection: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]]:
        return [row for row in self._all(collection) if predicate(row)]

    def _select(
        self, collection: str, predicate: str, parameters: tuple = (), *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Execute an internal SQL predicate with bound values, then decode matches only."""
        sql = f"SELECT payload FROM local_documents WHERE collection = ? AND ({predicate})"
        values = (collection, *parameters)
        if limit is not None:
            sql += " LIMIT ?"
            values = (*values, limit)
        with self._connection() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [self._loads(row["payload"]) for row in rows]

    def _delete_selected(self, collection: str, predicate: str, parameters: tuple = ()) -> int:
        """Delete by an internal indexed predicate inside the caller's transaction."""
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM local_documents WHERE collection = ? AND ({predicate})",
                (collection, *parameters),
            )
            return cursor.rowcount

    @_transactional
    def _delete_where(
        self, collection: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> int:
        rows = self._find(collection, predicate)
        if not rows:
            return 0
        keys = [self._key(row["_id"]) for row in rows]
        with self._write_lock, self._connection() as connection:
            connection.executemany(
                "DELETE FROM local_documents WHERE collection = ? AND document_key = ?",
                [(collection, key) for key in keys],
            )
        return len(keys)

    @staticmethod
    def public(document: dict[str, Any] | None) -> dict[str, Any] | None:
        if document is None:
            return None
        result = deepcopy(document)
        result["id"] = result.pop("_id")
        return result

    @_transactional
    def next_id(self, collection: str) -> int:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO local_counters(collection, value) VALUES (?, 1) "
                "ON CONFLICT(collection) DO UPDATE SET value = value + 1",
                (collection,),
            )
            row = connection.execute(
                "SELECT value FROM local_counters WHERE collection = ?", (collection,)
            ).fetchone()
        return int(row["value"])

    def metadata(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM local_metadata WHERE metadata_key = ?", (key,)
            ).fetchone()
        return self._loads(row["payload"]) if row else None

    @_transactional
    def set_metadata(self, key: str, values: dict[str, Any]) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO local_metadata(metadata_key, payload) VALUES (?, ?) "
                "ON CONFLICT(metadata_key) DO UPDATE SET payload = excluded.payload",
                (key, self._dumps(values)),
            )

    def keyword(self, keyword_id: int) -> dict[str, Any] | None:
        return self.public(self._get("keywords", keyword_id))

    def keywords(self) -> list[dict[str, Any]]:
        rows = sorted(
            self._all("keywords"),
            key=lambda row: _sort_value(row.get("updated_at")),
            reverse=True,
        )
        return [self.public(row) for row in rows]

    def due_keywords(self, now: datetime) -> list[dict[str, Any]]:
        return [
            self.public(row)
            for row in self._find(
                "keywords",
                lambda row: (
                    bool(row.get("enabled"))
                    and row.get("next_run_at") is not None
                    and row["next_run_at"] <= now
                ),
            )
        ]

    def keyword_name_exists(
        self, normalized_name: str, exclude_id: int | None = None
    ) -> bool:
        return any(
            row.get("normalized_name") == normalized_name and row["_id"] != exclude_id
            for row in self._all("keywords")
        )

    @_transactional
    def create_keyword(self, values: dict[str, Any]) -> dict[str, Any]:
        row = {"_id": self.next_id("keywords"), **deepcopy(values)}
        self._upsert("keywords", row)
        return self.public(row)

    @_transactional
    def update_keyword(
        self, keyword_id: int, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = self._get("keywords", keyword_id)
        if row is None:
            return None
        row.update(deepcopy(values))
        self._upsert("keywords", row)
        return self.public(row)

    @_transactional
    def next_session_number(self, keyword_id: int) -> int:
        row = self._get("keywords", keyword_id)
        if row is None:
            raise ValueError("Keyword not found")
        row["session_count"] = int(row.get("session_count", 0)) + 1
        self._upsert("keywords", row)
        return row["session_count"]

    @_transactional
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
        row = self._get("keywords", keyword_id)
        if row is None:
            return
        for channel in row.get("channels", []):
            if channel.get("id") == channel_id:
                channel.update(
                    {
                        "checkpoint": deepcopy(checkpoint),
                        "last_scanned_at": scanned_at,
                        "last_status": status,
                        "last_error": error,
                    }
                )
        row["updated_at"] = scanned_at
        self._upsert("keywords", row)

    @_transactional
    def update_source_checkpoint(
        self,
        keyword_id: int,
        source_id: str,
        operation: str,
        checkpoint: dict[str, Any],
        *,
        updated_at: Any,
    ) -> None:
        if (
            not source_id
            or not operation
            or any(character in source_id + operation for character in (".", "$"))
        ):
            raise ValueError("Unsafe source checkpoint key")
        row = self._get("keywords", keyword_id)
        if row is None:
            return
        row.setdefault("source_checkpoints", {}).setdefault(source_id, {})[
            operation
        ] = deepcopy(checkpoint)
        row["updated_at"] = updated_at
        self._upsert("keywords", row)

    @_transactional
    def delete_keyword(self, keyword_id: int) -> bool:
        if self._delete_where("keywords", lambda row: row["_id"] == keyword_id) == 0:
            return False
        batch_ids = {
            row["_id"]
            for row in self._find(
                "crawl_batches", lambda row: row.get("keyword_id") == keyword_id
            )
        }
        self._delete_where("source_runs", lambda row: row.get("batch_id") in batch_ids)
        self._delete_where("crawl_batches", lambda row: row["_id"] in batch_ids)
        content_ids = {
            row["content_item_id"]
            for row in self._find(
                "item_keyword_matches", lambda row: row.get("keyword_id") == keyword_id
            )
        }
        self._delete_where(
            "item_keyword_matches", lambda row: row.get("keyword_id") == keyword_id
        )
        remaining = {
            row["content_item_id"] for row in self._all("item_keyword_matches")
        }
        for content_item_id in content_ids - remaining:
            self.delete_item(content_item_id)
        return True

    @_transactional
    def delete_source_data(self, source_id: str) -> dict[str, int]:
        if not source_id or any(character in source_id for character in (".", "$")):
            raise ValueError("Unsafe source ID")
        content_ids = {
            row["_id"]
            for row in self._find(
                "content_items", lambda row: row.get("source_id") == source_id
            )
        }
        matches = self._delete_where(
            "item_keyword_matches",
            lambda row: row.get("content_item_id") in content_ids,
        )
        snapshots = self._delete_where(
            "metric_snapshots", lambda row: row.get("content_item_id") in content_ids
        )
        items = self._delete_where(
            "content_items", lambda row: row["_id"] in content_ids
        )
        comments = self._delete_where(
            "comments", lambda row: row.get("source_id") == source_id
        )
        source_runs = self._delete_where(
            "source_runs", lambda row: row.get("source_id") == source_id
        )
        keywords_reset = 0
        for keyword in self._all("keywords"):
            affected = source_id in keyword.get("source_checkpoints", {}) or any(
                channel.get("source_id") == source_id
                for channel in keyword.get("channels", [])
            )
            if not affected:
                continue
            keyword.setdefault("source_checkpoints", {}).pop(source_id, None)
            for channel in keyword.get("channels", []):
                if channel.get("source_id") == source_id:
                    channel.update(
                        {
                            "checkpoint": {},
                            "last_scanned_at": None,
                            "last_status": None,
                            "last_error": None,
                        }
                    )
            keyword["updated_at"] = datetime.now(UTC)
            self._upsert("keywords", keyword)
            keywords_reset += 1
        return {
            "content_items": items,
            "comments": comments,
            "item_matches": matches,
            "metric_snapshots": snapshots,
            "source_runs": source_runs,
            "keywords_reset": keywords_reset,
        }

    @_transactional
    def create_batch(
        self, batch: dict[str, Any], source_runs: list[dict[str, Any]]
    ) -> None:
        batch_row = deepcopy(batch)
        batch_row["_id"] = batch_row.pop("id")
        self._upsert("crawl_batches", batch_row)
        for source_run in source_runs:
            row = deepcopy(source_run)
            row["_id"] = row.pop("id")
            self._upsert("source_runs", row)

    def active_batch(self, keyword_id: int) -> dict[str, Any] | None:
        rows = self._select(
            "crawl_batches",
            "json_extract(payload, '$.keyword_id') = ? AND json_extract(payload, '$.state') IN ('queued', 'running')",
            (keyword_id,),
        )
        rows.sort(key=lambda row: _sort_value(row.get("started_at")), reverse=True)
        return self.public(rows[0]) if rows else None

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.public(self._get("crawl_batches", batch_id))
        if row is not None:
            row["source_runs"] = self.source_runs(batch_id)
        return row

    def batches(
        self, keyword_id: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = (self._all("crawl_batches") if keyword_id is None else self._select(
            "crawl_batches", "json_extract(payload, '$.keyword_id') = ?", (keyword_id,)
        ))
        rows.sort(key=lambda row: _sort_value(row.get("started_at")), reverse=True)
        result = []
        for raw in rows[:limit]:
            row = self.public(raw)
            row["source_runs"] = self.source_runs(row["id"])
            result.append(row)
        return result

    def batches_by_states(self, states: set[str]) -> list[dict[str, Any]]:
        return [
            self.public(row)
            for row in self._find(
                "crawl_batches", lambda row: row.get("state") in states
            )
        ]

    @_transactional
    def update_batch(self, batch_id: str, values: dict[str, Any]) -> None:
        row = self._get("crawl_batches", batch_id)
        if row:
            row.update(deepcopy(values))
            self._upsert("crawl_batches", row)

    @_transactional
    def prune_sessions(self, keyword_id: int, keep: int = 5) -> list[str]:
        rows = self._find(
            "crawl_batches",
            lambda row: (
                row.get("keyword_id") == keyword_id
                and row.get("state") not in {"queued", "running"}
            ),
        )
        rows.sort(
            key=lambda row: (
                _sort_value(row.get("finished_at")),
                _sort_value(row.get("started_at")),
            ),
            reverse=True,
        )
        expired_ids = [row["_id"] for row in rows[keep:]]
        if not expired_ids:
            return []
        expired = set(expired_ids)
        content_ids = {
            row["content_item_id"]
            for row in self._find(
                "item_keyword_matches",
                lambda row: (
                    row.get("keyword_id") == keyword_id
                    and row.get("session_id") in expired
                ),
            )
        }
        self._delete_where(
            "item_keyword_matches",
            lambda row: (
                row.get("keyword_id") == keyword_id and row.get("session_id") in expired
            ),
        )
        self._delete_where("source_runs", lambda row: row.get("batch_id") in expired)
        self._delete_where("crawl_batches", lambda row: row["_id"] in expired)
        remaining = {
            row["content_item_id"] for row in self._all("item_keyword_matches")
        }
        for content_item_id in content_ids - remaining:
            self.delete_item(content_item_id)
        return expired_ids

    def source_run(self, source_run_id: str) -> dict[str, Any] | None:
        return self.public(self._get("source_runs", source_run_id))

    def source_runs(self, batch_id: str) -> list[dict[str, Any]]:
        return [
            self.public(row)
            for row in self._select(
                "source_runs", "json_extract(payload, '$.batch_id') = ?", (batch_id,)
            )
        ]

    @_transactional
    def update_source_run(
        self,
        source_run_id: str,
        values: dict[str, Any],
        increments: dict[str, int] | None = None,
    ) -> None:
        row = self._get("source_runs", source_run_id)
        if row is None:
            return
        row.update(deepcopy(values))
        for key, increment in (increments or {}).items():
            row[key] = int(row.get(key, 0)) + increment
        self._upsert("source_runs", row)

    def item_by_source(self, source_id: str, external_id: str) -> dict[str, Any] | None:
        rows = self._select(
            "content_items",
            "json_extract(payload, '$.source_id') = ? AND json_extract(payload, '$.external_id') = ?",
            (source_id, external_id), limit=1,
        )
        return self.public(rows[0]) if rows else None

    def item(self, content_item_id: int) -> dict[str, Any] | None:
        return self.public(self._get("content_items", content_item_id))

    @_transactional
    def save_item(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock:
            values = deepcopy(values)
            content_item_id = values.pop("id", None)
            if content_item_id is None:
                existing = self.item_by_source(
                    str(values.get("source_id") or ""),
                    str(values.get("external_id") or ""),
                )
                content_item_id = (
                    existing["id"] if existing else self.next_id("content_items")
                )
            row = self._get("content_items", content_item_id) or {
                "_id": content_item_id
            }
            row.update(values)
            self._upsert("content_items", row)
            return self.public(row)

    @_transactional
    def ingest_content_bundle(
        self,
        item_values: dict[str, Any],
        snapshot_values: dict[str, Any],
        match_values: dict[str, Any],
        keyword_id: int,
        trend_score_fn: Callable[[dict[str, Any]], float] | None = None,
    ) -> dict[str, Any]:
        """Persist the item, snapshot and match on the same transaction connection."""
        item = self.save_item(item_values)
        snapshot = {**deepcopy(snapshot_values), "content_item_id": item["id"]}
        self.add_snapshot(snapshot)
        match = deepcopy(match_values)
        if trend_score_fn is not None and "trend_score" not in match:
            match["trend_score"] = trend_score_fn(item)
        self.save_match(item["id"], keyword_id, match)
        return item

    def comment_by_source(
        self, source_id: str, external_id: str
    ) -> dict[str, Any] | None:
        rows = self._select(
            "comments",
            "json_extract(payload, '$.source_id') = ? AND json_extract(payload, '$.external_id') = ?",
            (source_id, external_id), limit=1,
        )
        return self.public(rows[0]) if rows else None

    @_transactional
    def save_comment(self, values: dict[str, Any]) -> dict[str, Any]:
        values = deepcopy(values)
        source_id = str(values.get("source_id") or "").strip()
        external_id = str(values.get("external_id") or "").strip()
        content_external_id = str(values.get("content_external_id") or "").strip()
        if not source_id or not external_id or not content_external_id:
            raise ValueError("Comment identity fields cannot be empty")
        if (
            any(character in source_id for character in (".", "$"))
            or max(len(external_id), len(content_external_id)) > 256
        ):
            raise ValueError("Unsafe comment identity")
        if (
            values.get("parent_external_id") is not None
            and str(values["parent_external_id"]) == external_id
        ):
            raise ValueError("Comment cannot be its own parent")
        content = self.item_by_source(source_id, content_external_id)
        if content is None:
            raise ValueError("Comment parent content does not exist")
        if (
            values.get("content_item_id") is not None
            and values["content_item_id"] != content["id"]
        ):
            raise ValueError("Comment parent content identity is inconsistent")
        existing = self.comment_by_source(source_id, external_id)
        now = values.get("last_seen_at") or datetime.now(UTC)
        first_seen_at = values.pop("first_seen_at", None) or now
        identifier = existing["id"] if existing else self.next_id("comments")
        row = self._get("comments", identifier) or {
            "_id": identifier,
            "first_seen_at": first_seen_at,
        }
        values.update(
            {
                "source_id": source_id,
                "external_id": external_id,
                "content_external_id": content_external_id,
                "content_item_id": content["id"],
                "body": str(values.get("body") or "")[:4000],
                "author_pseudonym": str(values.get("author_pseudonym") or "")[:128],
                "last_seen_at": now,
            }
        )
        row.update(values)
        self._upsert("comments", row)
        return self.public(row)

    def comments_for_content(
        self,
        source_id: str,
        content_external_id: str,
        *,
        limit: int = 200,
        root_external_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Comment list limit must be between 1 and 1000")
        rows = self._select(
            "comments",
            "json_extract(payload, '$.source_id') = ? AND json_extract(payload, '$.content_external_id') = ?"
            + (" AND json_extract(payload, '$.root_external_id') = ?" if root_external_id is not None else ""),
            (source_id, content_external_id) + ((root_external_id,) if root_external_id is not None else ()),
        )
        rows.sort(key=lambda row: (_sort_value(row.get("published_at")), row["_id"]))
        return [self.public(row) for row in rows[:limit]]

    @_transactional
    def delete_comments_for_content(
        self, source_id: str, content_external_id: str
    ) -> int:
        return self._delete_selected(
            "comments",
            "json_extract(payload, '$.source_id') = ? AND json_extract(payload, '$.content_external_id') = ?",
            (source_id, content_external_id),
        )

    @_transactional
    def delete_item(self, content_item_id: int) -> None:
        item = self._get("content_items", content_item_id)
        if item:
            self.delete_comments_for_content(
                str(item.get("source_id") or ""), str(item.get("external_id") or "")
            )
        self._delete_selected("content_items", "document_key = ?", (self._key(content_item_id),))
        self._delete_selected(
            "item_keyword_matches",
            "json_extract(payload, '$.content_item_id') = ?", (content_item_id,),
        )
        self._delete_selected(
            "metric_snapshots",
            "json_extract(payload, '$.content_item_id') = ?", (content_item_id,),
        )

    def match(self, content_item_id: int, keyword_id: int) -> dict[str, Any] | None:
        rows = self._select(
            "item_keyword_matches",
            "json_extract(payload, '$.keyword_id') = ? AND json_extract(payload, '$.content_item_id') = ?",
            (keyword_id, content_item_id), limit=1,
        )
        return self.public(rows[0]) if rows else None

    def has_external_match(
        self, source_id: str, external_id: str, keyword_id: int
    ) -> bool:
        item = self.item_by_source(source_id, external_id)
        return bool(item and self.match(item["id"], keyword_id))

    @_transactional
    def save_match(
        self, content_item_id: int, keyword_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        existing = self.match(content_item_id, keyword_id)
        identifier = (
            existing["id"] if existing else self.next_id("item_keyword_matches")
        )
        row = self._get("item_keyword_matches", identifier) or {
            "_id": identifier,
            "content_item_id": content_item_id,
            "keyword_id": keyword_id,
        }
        row.update(deepcopy(values))
        self._upsert("item_keyword_matches", row)
        return self.public(row)

    @_transactional
    def delete_match(self, content_item_id: int, keyword_id: int) -> None:
        self._delete_selected(
            "item_keyword_matches",
            "json_extract(payload, '$.keyword_id') = ? AND json_extract(payload, '$.content_item_id') = ?",
            (keyword_id, content_item_id),
        )
        if not self.has_matches_for_item(content_item_id):
            self.delete_item(content_item_id)

    @_transactional
    def add_snapshot(self, values: dict[str, Any]) -> None:
        rows = self._select(
            "metric_snapshots",
            "json_extract(payload, '$.content_item_id') = ?",
            (values["content_item_id"],),
        )
        rows = [row for row in rows if row.get("captured_at") == values["captured_at"]]
        identifier = rows[0]["_id"] if rows else self.next_id("metric_snapshots")
        row = rows[0] if rows else {"_id": identifier}
        row.update(deepcopy(values))
        self._upsert("metric_snapshots", row)

    def snapshots(
        self, content_item_id: int, descending: bool = False, limit: int = 0
    ) -> list[dict[str, Any]]:
        rows = self._select(
            "metric_snapshots",
            "json_extract(payload, '$.content_item_id') = ?", (content_item_id,),
        )
        rows.sort(
            key=lambda row: _sort_value(row.get("captured_at")), reverse=descending
        )
        if limit:
            rows = rows[:limit]
        return [self.public(row) for row in rows]

    def item_matches(
        self,
        keyword_id: int,
        positive_only: bool = False,
        source_id: str | None = None,
        min_relevance: float = 0,
        session_id: str | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        clauses = ["m.collection = 'item_keyword_matches'", "json_extract(m.payload, '$.keyword_id') = ?"]
        values: list[Any] = [keyword_id]
        if session_id is not None:
            clauses.append("json_extract(m.payload, '$.session_id') = ?")
            values.append(session_id)
        if min_relevance > 0:
            clauses.append("COALESCE(json_extract(m.payload, '$.relevance_score'), 0) >= ?")
            values.append(min_relevance)
        elif positive_only:
            clauses.append("COALESCE(json_extract(m.payload, '$.relevance_score'), 0) > 0")
        if source_id is not None:
            clauses.append("json_extract(i.payload, '$.source_id') = ?")
            values.append(source_id)
        sql = (
            "SELECT i.payload AS item, m.payload AS matched FROM local_documents m "
            "JOIN local_documents i ON i.collection = 'content_items' "
            "AND i.document_key = 'i:' || json_extract(m.payload, '$.content_item_id') "
            "WHERE " + " AND ".join(clauses)
        )
        with self._connection() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [(self.public(self._loads(row["item"])), self.public(self._loads(row["matched"]))) for row in rows]

    def query_items(self, query, *, limit=None, offset=0):
        return select_items(self, query, limit, offset)

    def item_query_revision(self) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT revision FROM local_item_revision WHERE id=1").fetchone()[0]

    def source_metric_values(self, source_id: str) -> list[dict[str, int]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT json_extract(payload, '$.metrics') FROM local_documents "
                "WHERE collection = 'content_items' AND json_extract(payload, '$.source_id') = ?",
                (source_id,),
            ).fetchall()
        return [json.loads(row[0]) if row[0] is not None else {} for row in rows]

    def source_item_ids(self, source_id: str) -> list[int]:
        with self._connection() as connection:
            return [row[0] for row in connection.execute(
                "SELECT json_extract(payload, '$._id') FROM local_documents "
                "WHERE collection = 'content_items' AND json_extract(payload, '$.source_id') = ?",
                (source_id,),
            )]

    def source_snapshot_pairs(self, source_id: str) -> list[list[dict[str, Any]]]:
        from .services.snapshot_pairs import latest_pairs

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT snapshots.payload FROM local_documents AS snapshots "
                "JOIN local_documents AS items ON items.collection='content_items' "
                "AND items.document_key='i:' || json_extract(snapshots.payload, '$.content_item_id') "
                "WHERE snapshots.collection='metric_snapshots' "
                "AND json_extract(items.payload, '$.source_id')=? "
                "ORDER BY snapshots.rowid", (source_id,),
            )
            return latest_pairs(self.public(self._loads(row[0])) for row in rows)

    def content_item_ids_before(self, cutoff: datetime) -> list[int]:
        return [
            row["_id"]
            for row in self._find(
                "content_items",
                lambda row: (
                    row.get("last_seen_at") is not None and row["last_seen_at"] < cutoff
                ),
            )
        ]

    @_transactional
    def delete_irrelevant_matches(self) -> tuple[int, set[int]]:
        matches = self._find(
            "item_keyword_matches", lambda row: row.get("relevance_score", 0) <= 0
        )
        candidate_ids = {row["content_item_id"] for row in matches}
        identifiers = {row["_id"] for row in matches}
        self._delete_where(
            "item_keyword_matches", lambda row: row["_id"] in identifiers
        )
        return len(matches), candidate_ids

    def has_matches_for_item(self, content_item_id: int) -> bool:
        return bool(self._select(
            "item_keyword_matches", "json_extract(payload, '$.content_item_id') = ?",
            (content_item_id,), limit=1,
        ))

    def recent_items(
        self, source_ids: set[str], limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self._find(
            "content_items", lambda row: row.get("source_id") in source_ids
        )
        rows.sort(key=lambda row: _sort_value(row.get("published_at")), reverse=True)
        return [self.public(row) for row in rows[:limit]]
