"""Additive indexes for the existing JSON document schema; payloads stay unchanged."""

from __future__ import annotations

import sqlite3

INDEX_VERSION = "local-query-indexes-v1"
INDEXES = {
    "ix_content_source_external": ("content_items", ("source_id", "external_id")),
    "ix_comment_source_external": ("comments", ("source_id", "external_id")),
    "ix_comment_parent": ("comments", ("source_id", "content_external_id", "root_external_id")),
    "ix_match_keyword_item": ("item_keyword_matches", ("keyword_id", "content_item_id")),
    "ix_match_keyword_session": ("item_keyword_matches", ("keyword_id", "session_id")),
    "ix_match_content": ("item_keyword_matches", ("content_item_id",)),
    "ix_snapshot_content": ("metric_snapshots", ("content_item_id",)),
    "ix_run_batch": ("source_runs", ("batch_id",)),
    "ix_batch_keyword": ("crawl_batches", ("keyword_id",)),
}


def ensure_query_indexes(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM local_metadata WHERE metadata_key = ?", (INDEX_VERSION,)
    ).fetchone():
        return
    for name, (collection, fields) in INDEXES.items():
        expressions = ", ".join(f"json_extract(payload, '$.{field}')" for field in fields)
        # All identifiers and predicates come from the static schema above.
        # Non-unique indexes preserve legacy duplicates without deleting data;
        # store transactions serialize identity lookup/upsert for new writes.
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON local_documents ({expressions}) "
            f"WHERE collection = '{collection}'"
        )
    connection.execute(
        "INSERT INTO local_metadata(metadata_key, payload) VALUES (?, ?)",
        (INDEX_VERSION, '{"version":1}'),
    )
