"""Storage contract shared by application services and HTTP dependencies.

SQLite mutations are atomic. Mongo standalone bundles guarantee ordered writes
only; callers must allow a partially committed bundle after a storage error.
Optional query revisions are a capability checked separately by insight caches.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PersistenceStore(Protocol):
    storage_name: str

    @property
    def is_available(self) -> bool: ...

    def ping(self) -> bool: ...

    def ping_cached(self, ttl_seconds: float = 5.0) -> bool: ...

    def initialize(self, ping: bool = False) -> None: ...

    def next_id(self, collection: str) -> int: ...

    @staticmethod
    def public(doc: dict[str, Any] | None) -> dict[str, Any] | None: ...

    def keyword(self, keyword_id: int) -> dict[str, Any] | None: ...

    def keywords(self) -> list[dict[str, Any]]: ...

    def keyword_name_exists(
        self, normalized_name: str, exclude_id: int | None = None
    ) -> bool: ...

    def create_keyword(self, values: dict[str, Any]) -> dict[str, Any]: ...

    def update_keyword(
        self, keyword_id: int, values: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def next_session_number(self, keyword_id: int) -> int: ...

    def update_channel_checkpoint(
        self,
        keyword_id: int,
        channel_id: str,
        checkpoint: dict[str, Any],
        *,
        scanned_at: Any,
        status: str,
        error: str | None = None,
    ) -> None: ...

    def update_source_checkpoint(
        self,
        keyword_id: int,
        source_id: str,
        operation: str,
        checkpoint: dict[str, Any],
        *,
        updated_at: Any,
    ) -> None: ...

    def delete_keyword(self, keyword_id: int) -> bool: ...

    def delete_source_data(self, source_id: str) -> dict[str, int]: ...

    def create_batch(
        self, batch: dict[str, Any], source_runs: list[dict[str, Any]]
    ) -> None: ...

    def active_batch(self, keyword_id: int) -> dict[str, Any] | None: ...

    def batch(self, batch_id: str) -> dict[str, Any] | None: ...

    def batches(
        self, keyword_id: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    def update_batch(self, batch_id: str, values: dict[str, Any]) -> None: ...

    def prune_sessions(self, keyword_id: int, keep: int = 5) -> list[str]: ...

    def source_run(self, source_run_id: str) -> dict[str, Any] | None: ...

    def source_runs(self, batch_id: str) -> list[dict[str, Any]]: ...

    def update_source_run(
        self,
        source_run_id: str,
        values: dict[str, Any],
        increments: dict[str, int] | None = None,
    ) -> None: ...

    def item_by_source(
        self, source_id: str, external_id: str
    ) -> dict[str, Any] | None: ...

    def item(self, content_item_id: int) -> dict[str, Any] | None: ...

    def save_item(self, values: dict[str, Any]) -> dict[str, Any]: ...

    def query_items(self, query, *, limit=None, offset=0): ...

    def source_snapshot_pairs(self, source_id: str) -> list[list[dict[str, Any]]]: ...

    def ingest_content_bundle(
        self,
        item_values: dict[str, Any],
        snapshot_values: dict[str, Any],
        match_values: dict[str, Any],
        keyword_id: int,
        trend_score_fn: Callable[[dict[str, Any]], float] | None = None,
    ) -> dict[str, Any]: ...

    def comment_by_source(
        self, source_id: str, external_id: str
    ) -> dict[str, Any] | None: ...

    def save_comment(self, values: dict[str, Any]) -> dict[str, Any]: ...

    def comments_for_content(
        self,
        source_id: str,
        content_external_id: str,
        *,
        limit: int = 200,
        root_external_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def delete_comments_for_content(
        self, source_id: str, content_external_id: str
    ) -> int: ...

    def delete_item(self, content_item_id: int) -> None: ...

    def match(self, content_item_id: int, keyword_id: int) -> dict[str, Any] | None: ...

    def has_external_match(
        self, source_id: str, external_id: str, keyword_id: int
    ) -> bool: ...

    def save_match(
        self, content_item_id: int, keyword_id: int, values: dict[str, Any]
    ) -> dict[str, Any]: ...

    def delete_match(self, content_item_id: int, keyword_id: int) -> None: ...

    def add_snapshot(self, values: dict[str, Any]) -> None: ...

    def snapshots(
        self, content_item_id: int, descending: bool = False, limit: int = 0
    ) -> list[dict[str, Any]]: ...

    def item_matches(
        self,
        keyword_id: int,
        positive_only: bool = False,
        source_id: str | None = None,
        min_relevance: float = 0,
        session_id: str | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]: ...

    def source_metric_values(self, source_id: str) -> list[dict[str, int]]: ...

    def source_item_ids(self, source_id: str) -> list[int]: ...

    def metadata(self, key: str) -> dict[str, Any] | None: ...

    def set_metadata(self, key: str, values: dict[str, Any]) -> None: ...

    def due_keywords(self, now: datetime) -> list[dict[str, Any]]: ...

    def content_item_ids_before(self, cutoff: datetime) -> list[int]: ...

    def batches_by_states(self, states: set[str]) -> list[dict[str, Any]]: ...

    def delete_irrelevant_matches(self) -> tuple[int, set[int]]: ...

    def has_matches_for_item(self, content_item_id: int) -> bool: ...

    def recent_items(
        self, source_ids: set[str], limit: int = 50
    ) -> list[dict[str, Any]]: ...
