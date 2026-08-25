from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import main
from app.sqlite_store import SQLiteStore


def test_sqlite_store_persists_keywords_across_reopen(tmp_path) -> None:
    path = tmp_path / "content-bot.db"
    storage = SQLiteStore(path)
    storage.initialize()
    now = datetime.now(UTC)

    created = storage.create_keyword(
        {
            "name": "Neverness to Everness",
            "normalized_name": "neverness to everness",
            "include_terms": ["NTE"],
            "exclude_terms": [],
            "source_ids": ["youtube"],
            "channels": [],
            "enabled": True,
            "interval_minutes": 360,
            "max_items_per_source": 100,
            "next_run_at": now - timedelta(minutes=1),
            "created_at": now,
            "updated_at": now,
        }
    )

    reopened = SQLiteStore(path)
    reopened.initialize()

    assert reopened.is_available is True
    assert reopened.keyword(created["id"])["name"] == "Neverness to Everness"
    assert reopened.due_keywords(now)[0]["id"] == created["id"]
    assert reopened.keyword_name_exists("neverness to everness") is True


def test_sqlite_store_imports_legacy_database_once(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE keywords (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                include_terms_json TEXT NOT NULL,
                exclude_terms_json TEXT NOT NULL,
                source_ids_json TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                interval_minutes INTEGER NOT NULL,
                max_items_per_source INTEGER NOT NULL,
                next_run_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO keywords VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "Điện Thoại", json.dumps(["mobile"]), "[]", "[]", 1, 60, 100, now, now, now),
        )

    storage = SQLiteStore(path)
    storage.initialize()
    storage.initialize()

    keywords = storage.keywords()
    assert len(keywords) == 1
    assert keywords[0]["normalized_name"] == "đien thoai"
    assert keywords[0]["channels"] == []
    assert keywords[0]["include_terms"] == ["mobile"]


def test_sqlite_store_cascades_local_content_and_comments(tmp_path) -> None:
    storage = SQLiteStore(tmp_path / "content-bot.db")
    storage.initialize()
    now = datetime.now(UTC)
    item = storage.save_item(
        {
            "source_id": "youtube",
            "external_id": "video-1",
            "title": "Video",
            "last_seen_at": now,
        }
    )
    storage.save_comment(
        {
            "source_id": "youtube",
            "external_id": "comment-1",
            "content_external_id": "video-1",
            "body": "local comment",
            "last_seen_at": now,
        }
    )

    assert len(storage.comments_for_content("youtube", "video-1")) == 1
    storage.delete_item(item["id"])
    assert storage.item(item["id"]) is None
    assert storage.comments_for_content("youtube", "video-1") == []


def test_keyword_api_works_without_mongodb(tmp_path, monkeypatch) -> None:
    storage = SQLiteStore(tmp_path / "content-bot.db")
    monkeypatch.setattr(main, "store", storage)
    main.run_manager.store = storage

    with TestClient(main.app) as client:
        created = client.post(
            "/api/v1/keywords", json={"name": "Local topic"}
        )
        listed = client.get("/api/v1/keywords")
        ready = client.get("/api/v1/ready")

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "Local topic"
    assert ready.json()["storage"] == "sqlite"
    assert ready.json()["database_ready"] is True
