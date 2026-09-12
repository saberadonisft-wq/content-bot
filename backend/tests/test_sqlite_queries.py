from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.services.sqlite_backup import backup_sqlite
from app.sqlite_indexes import INDEX_VERSION, INDEXES
from app.sqlite_store import SQLiteStore


def test_query_index_migration_preserves_payloads_and_is_repeatable(tmp_path):
    path = tmp_path / "legacy-documents.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE local_documents (collection TEXT, document_key TEXT, payload TEXT, PRIMARY KEY(collection, document_key))"
        )
        # Existing duplicate logical IDs are retained, not silently merged/deleted.
        payload = '{"_id":1,"source_id":"web","external_id":"a","custom":"keep"}'
        connection.execute(
            "INSERT INTO local_documents VALUES ('content_items', 'i:1', ?)", (payload,)
        )
        connection.execute(
            "INSERT INTO local_documents VALUES ('content_items', 'i:2', ?)",
            (payload.replace('"_id":1', '"_id":2'),),
        )
    store = SQLiteStore(path)
    store.initialize()
    store.initialize()
    with store._connection() as connection:
        assert (
            connection.execute(
                "SELECT payload FROM local_documents WHERE document_key='i:1'"
            ).fetchone()[0]
            == payload
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM local_documents WHERE collection='content_items'"
            ).fetchone()[0]
            == 2
        )
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(local_documents)")
        }
    assert set(INDEXES) <= indexes
    assert store.metadata(INDEX_VERSION) == {"version": 1}


def test_identity_query_uses_expression_index(tmp_path):
    store = SQLiteStore(tmp_path / "query.db")
    store.initialize()
    with store._connection() as connection:
        plan = list(
            connection.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM local_documents WHERE collection = ? "
                "AND json_extract(payload, '$.source_id') = ? AND json_extract(payload, '$.external_id') = ? LIMIT 1",
                ("content_items", "web", "a"),
            )
        )
    assert any(
        "SEARCH" in row[3] and "ix_content_source_external" in row[3] for row in plan
    )


def test_hot_queries_do_not_scan_collections_or_open_per_match(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "queries.db")
    store.initialize()
    with store._transaction():
        for n in range(30):
            item = store.save_item({"source_id": "web", "external_id": str(n)})
            store.save_match(item["id"], 1, {"relevance_score": 80})
            store.add_snapshot(
                {"content_item_id": item["id"], "captured_at": datetime.now(UTC)}
            )

    def forbid(_collection):
        raise AssertionError("Hot query scanned the entire collection")

    monkeypatch.setattr(store, "_all", forbid)
    connects = 0
    original = store._connect

    def track():
        nonlocal connects
        connects += 1
        return original()

    monkeypatch.setattr(store, "_connect", track)
    assert len(store.item_matches(1)) == 30
    assert connects == 1
    assert store.item_by_source("web", "0")["id"] == 1
    assert store.match(1, 1)["relevance_score"] == 80
    assert len(store.snapshots(1)) == 1
    assert store.has_matches_for_item(1)
    assert set(store.source_item_ids("web")) == set(range(1, 31))
    assert store.source_metric_values("web") == [{}] * 30
    store.delete_match(1, 1)
    assert store.item(1) is None
    assert store.snapshots(1) == []


def test_wal_backup_can_reopen_and_migrate_without_losing_committed_rows(tmp_path):
    source = SQLiteStore(tmp_path / "source.db")
    source.initialize()
    source.create_keyword({"name": "Backed up"})
    backup_path = tmp_path / "backup.db"
    # Keep a connection open so the committed content actually remains in WAL.
    with closing(source._connect()) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint = 0")
        keeper.execute("BEGIN")
        keeper.execute("SELECT COUNT(*) FROM local_documents").fetchone()
        source.create_keyword({"name": "Committed in WAL"})
        assert source.path.with_suffix(".db-wal").stat().st_size > 0
        backup_sqlite(source.path, backup_path)
    restored = SQLiteStore(backup_path)
    restored.initialize()
    assert restored.keywords() == source.keywords()
    assert restored.metadata(INDEX_VERSION) == source.metadata(INDEX_VERSION)


def test_backup_refuses_overwrite_and_missing_source(tmp_path):
    source = SQLiteStore(tmp_path / "source.db")
    source.initialize()
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"existing backup")
    with pytest.raises(FileExistsError):
        backup_sqlite(source.path, destination)
    assert destination.read_bytes() == b"existing backup"
    with pytest.raises(FileNotFoundError):
        backup_sqlite(tmp_path / "missing.db", tmp_path / "new.db")
    assert not (tmp_path / "missing.db").exists()


def test_snapshot_population_does_not_query_each_source_item(tmp_path):
    from datetime import UTC, datetime, timedelta

    from app.sqlite_store import SQLiteStore

    storage = SQLiteStore(tmp_path / "population.db")
    storage.initialize()
    for identifier in range(30):
        saved = storage.save_item({"source_id": "web", "external_id": str(identifier)})
        if identifier == 0:
            for hour in range(3):
                storage.add_snapshot(
                    {
                        "content_item_id": saved["id"],
                        "captured_at": datetime(2026, 9, 12, tzinfo=UTC)
                        + timedelta(hours=hour),
                    }
                )
    statements = []
    with storage._transaction(), storage._connection() as connection:
        connection.set_trace_callback(statements.append)
        pairs = storage.source_snapshot_pairs("web")
        connection.set_trace_callback(None)
    assert len(pairs) == 1 and len(pairs[0]) == 2
    assert (
        len(
            [
                statement
                for statement in statements
                if statement.lstrip().upper().startswith("SELECT")
            ]
        )
        == 1
    )
