from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import ContentItem

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backup_sqlite
import delete_local_data
import prune_sqlite
import restore_sqlite


def write_marker(path: Path, marker: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()


def read_marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM marker").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def test_sqlite_path_rejects_non_sqlite_urls() -> None:
    try:
        backup_sqlite.sqlite_path("mongodb://localhost/content-bot")
    except ValueError as error:
        assert "sqlite" in str(error).lower()
    else:
        raise AssertionError("Expected a non-SQLite URL to be rejected")


def test_restore_replaces_configured_database_after_integrity_check(tmp_path, monkeypatch) -> None:
    current = tmp_path / "current.db"
    backup = tmp_path / "backup.db"
    write_marker(current, "current-data")
    write_marker(backup, "backup-data")

    monkeypatch.setattr(restore_sqlite.settings, "mongodb_uri", None)
    monkeypatch.setattr(restore_sqlite.settings, "content_bot_database_url", f"sqlite:///{current.as_posix()}")
    monkeypatch.setattr(restore_sqlite, "local_api_is_running", lambda: False)
    monkeypatch.setattr(sys, "argv", ["restore_sqlite.py", "--source", str(backup), "--replace-current"])

    assert restore_sqlite.main() == 0
    assert read_marker(current) == "backup-data"
    assert read_marker(backup) == "backup-data"


def test_restore_refuses_without_explicit_replacement_acknowledgement(tmp_path, monkeypatch) -> None:
    backup = tmp_path / "backup.db"
    write_marker(backup, "safe")
    monkeypatch.setattr(restore_sqlite.settings, "mongodb_uri", None)
    monkeypatch.setattr(sys, "argv", ["restore_sqlite.py", "--source", str(backup)])

    try:
        restore_sqlite.main()
    except PermissionError as error:
        assert "replace-current" in str(error)
    else:
        raise AssertionError("Expected restore acknowledgement to be required")


def test_prune_deletes_only_expired_items_after_apply(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    old_seen = datetime.now(UTC) - timedelta(days=91)
    fresh_seen = datetime.now(UTC) - timedelta(days=2)
    with test_session() as session:
        session.add_all(
            [
                ContentItem(source_id="test", external_id="old", canonical_url="https://example.test/old", last_seen_at=old_seen),
                ContentItem(source_id="test", external_id="fresh", canonical_url="https://example.test/fresh", last_seen_at=fresh_seen),
            ]
        )
        session.commit()

    monkeypatch.setattr(prune_sqlite.settings, "mongodb_uri", None)
    monkeypatch.setattr(prune_sqlite, "SessionLocal", test_session)
    monkeypatch.setattr(prune_sqlite, "local_api_is_running", lambda: False)
    monkeypatch.setattr(sys, "argv", ["prune_sqlite.py", "--days", "90", "--apply"])

    assert prune_sqlite.main() == 0
    with test_session() as session:
        remaining = session.scalars(select(ContentItem.external_id)).all()
    assert remaining == ["fresh"]


def test_delete_local_data_removes_only_validated_workspace_data(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "content-bot.db"
    write_marker(database, "delete-me")
    profile = data_dir / "browser-profile" / "bilibili"
    profile.mkdir(parents=True)
    (profile / "session.txt").write_text("local", encoding="utf-8")

    monkeypatch.setattr(delete_local_data.settings, "content_bot_data_dir", data_dir)
    monkeypatch.setattr(delete_local_data.settings, "content_bot_database_url", f"sqlite:///{database.as_posix()}")
    monkeypatch.setattr(delete_local_data, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(delete_local_data, "local_api_is_running", lambda: False)
    monkeypatch.setattr(sys, "argv", ["delete_local_data.py", "--confirm-delete-local-data"])

    assert delete_local_data.main() == 0
    assert not data_dir.exists()
