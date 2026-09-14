"""Rebuildable SQL read model; original documents remain the source of truth."""

from __future__ import annotations

import json
from contextlib import contextmanager

from .services.item_query import (
    ANALYSIS_VERSION,
    ItemQuery,
    analysis_fingerprint,
    analyze_item,
    search_text,
    sort_timestamp,
)

SCHEMA_KEY = "local-item-query-v1"


def initialize_item_query(connection):
    # Executed before the initialization transaction begins (executescript commits).
    # Replace old triggers atomically. An outer UPSERT overrides a trigger's
    # INSERT OR IGNORE conflict policy, so avoid the duplicate insert altogether.
    connection.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS local_item_analysis (
            document_key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            version TEXT NOT NULL,
            analysis TEXT NOT NULL,
            language TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            topics TEXT NOT NULL,
            search_text TEXT NOT NULL,
            sort_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_item_dirty (document_key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS local_item_revision (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
        INSERT OR IGNORE INTO local_item_revision VALUES (1, 0);
        CREATE TRIGGER IF NOT EXISTS item_revision_insert AFTER INSERT ON local_documents
        WHEN NEW.collection IN ('content_items', 'item_keyword_matches')
        BEGIN UPDATE local_item_revision SET revision=revision+1 WHERE id=1; END;
        CREATE TRIGGER IF NOT EXISTS item_revision_update AFTER UPDATE ON local_documents
        WHEN NEW.collection IN ('content_items', 'item_keyword_matches')
        BEGIN UPDATE local_item_revision SET revision=revision+1 WHERE id=1; END;
        CREATE TRIGGER IF NOT EXISTS item_revision_delete AFTER DELETE ON local_documents
        WHEN OLD.collection IN ('content_items', 'item_keyword_matches')
        BEGIN UPDATE local_item_revision SET revision=revision+1 WHERE id=1; END;
        DROP TRIGGER IF EXISTS item_query_insert;
        DROP TRIGGER IF EXISTS item_query_update;
        CREATE TRIGGER item_query_insert AFTER INSERT ON local_documents
        WHEN NEW.collection = 'content_items'
          AND NOT EXISTS (SELECT 1 FROM local_item_dirty WHERE document_key = NEW.document_key)
        BEGIN INSERT INTO local_item_dirty VALUES (NEW.document_key); END;
        CREATE TRIGGER item_query_update AFTER UPDATE ON local_documents
        WHEN NEW.collection = 'content_items'
          AND NOT EXISTS (SELECT 1 FROM local_item_dirty WHERE document_key = NEW.document_key)
        BEGIN INSERT INTO local_item_dirty VALUES (NEW.document_key); END;
        CREATE TRIGGER IF NOT EXISTS item_query_delete AFTER DELETE ON local_documents
        WHEN OLD.collection = 'content_items'
        BEGIN
            DELETE FROM local_item_analysis WHERE document_key = OLD.document_key;
            DELETE FROM local_item_dirty WHERE document_key = OLD.document_key;
        END;
        COMMIT;
    """)


def queue_existing_analysis(connection):
    row = connection.execute("SELECT payload FROM local_metadata WHERE metadata_key=?", (SCHEMA_KEY,)).fetchone()
    version = json.dumps({"analysis_version": ANALYSIS_VERSION})
    if row and row[0] == version:
        return
    connection.execute("INSERT OR IGNORE INTO local_item_dirty SELECT document_key FROM local_documents WHERE collection='content_items'")
    connection.execute("INSERT INTO local_metadata VALUES (?, ?) ON CONFLICT(metadata_key) DO UPDATE SET payload=excluded.payload", (SCHEMA_KEY, version))


def update_item_analysis(connection, key: str, item: dict):
    fingerprint = analysis_fingerprint(item)
    previous = connection.execute("SELECT fingerprint, version, analysis FROM local_item_analysis WHERE document_key=?", (key,)).fetchone()
    analysis = (json.loads(previous[2]) if previous and previous[0] == fingerprint and previous[1] == ANALYSIS_VERSION else analyze_item(item))
    connection.execute("""
        INSERT INTO local_item_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_key) DO UPDATE SET
          fingerprint=excluded.fingerprint, version=excluded.version, analysis=excluded.analysis,
          language=excluded.language, sentiment=excluded.sentiment, topics=excluded.topics,
          search_text=excluded.search_text, sort_at=excluded.sort_at
    """, (key, fingerprint, ANALYSIS_VERSION, json.dumps(analysis, ensure_ascii=False),
           analysis["language"]["code"], analysis["sentiment"]["label"],
           json.dumps([topic["id"] for topic in analysis["topics"]]), search_text(item), sort_timestamp(item)))
    connection.execute("DELETE FROM local_item_dirty WHERE document_key=?", (key,))


def refresh_item_analysis(store):
    # Migration/older application writes are consumed in bounded batches.
    while True:
        with store._connection() as connection:
            if not connection.execute("SELECT 1 FROM local_item_dirty LIMIT 1").fetchone():
                return
        with store._transaction() as connection:
            rows = connection.execute("""
                SELECT d.document_key, i.payload FROM local_item_dirty d
                LEFT JOIN local_documents i ON i.collection='content_items' AND i.document_key=d.document_key
                LIMIT 128
            """).fetchall()
            for key, payload in rows:
                if payload is None:
                    connection.execute("DELETE FROM local_item_dirty WHERE document_key=?", (key,))
                else:
                    update_item_analysis(connection, key, store._loads(payload))


def select_items(store, query: ItemQuery, limit: int | None, offset: int):
    clauses = ["m.collection='item_keyword_matches'", "json_extract(m.payload, '$.keyword_id')=?",
               "COALESCE(json_extract(m.payload, '$.relevance_score'), 0)>0"]
    params = [query.keyword_id]
    for value, expression in (
        (query.source_id or None, "json_extract(i.payload, '$.source_id')"),
        (query.session_id or None, "json_extract(m.payload, '$.session_id')"),
        (query.language or None, "a.language"), (query.sentiment or None, "a.sentiment"),
    ):
        if value is not None:
            clauses.append(expression + "=?")
            params.append(value)
    if query.min_relevance > 0:
        clauses.append("json_extract(m.payload, '$.relevance_score')>=?")
        params.append(query.min_relevance)
    if query.needle:
        clauses.append("instr(a.search_text, ?)>0")
        params.append(query.needle)
    if query.topic:
        clauses.append("EXISTS (SELECT 1 FROM json_each(a.topics) WHERE value=?)")
        params.append(query.topic)
    source = """
        FROM local_documents m
        JOIN local_item_analysis a ON a.document_key='i:' || json_extract(m.payload, '$.content_item_id')
        """
    if query.source_id:
        source += " JOIN local_documents i ON i.collection='content_items' AND i.document_key=a.document_key "
    source += " WHERE " + " AND ".join(clauses)
    with consistent_connection(store) as connection:
        # Count/page share a read snapshot, so concurrent ingestion cannot change
        # the reported total between these two statements.
        if not connection.in_transaction:
            connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*) " + source, params).fetchone()[0]
        rows = connection.execute(
            "WITH page AS MATERIALIZED (SELECT m.payload AS match_payload, a.analysis, a.document_key, a.sort_at " + source +
            " ORDER BY COALESCE(json_extract(m.payload, '$.trend_score'), 0) DESC, a.sort_at DESC, "
            "json_extract(m.payload, '$.content_item_id') ASC LIMIT ? OFFSET ?) "
            "SELECT i.payload, page.match_payload, page.analysis FROM page "
            "JOIN local_documents i ON i.collection='content_items' AND i.document_key=page.document_key "
            "ORDER BY COALESCE(json_extract(page.match_payload, '$.trend_score'), 0) DESC, page.sort_at DESC, "
            "json_extract(i.payload, '$._id') ASC",
            (*params, -1 if limit is None else limit, offset),
        ).fetchall()
    return total, [(store.public(store._loads(item)), store.public(store._loads(match)), json.loads(analysis))
                   for item, match, analysis in rows]


@contextmanager
def consistent_connection(store):
    """Include invalidation in the read snapshot, even with older writers."""
    while True:
        refresh_item_analysis(store)
        with store._connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM local_item_dirty LIMIT 1").fetchone():
                # A legacy writer committed between refresh and BEGIN. Release
                # this snapshot before draining its changes and reading again.
                continue
            yield connection
            return
