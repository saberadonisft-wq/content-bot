from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.mongo import MongoStore

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backup_mongodb
import prune_mongodb
import restore_mongodb


def configure_modules(storage: MongoStore, monkeypatch) -> None:
    for module in (backup_mongodb, prune_mongodb, restore_mongodb):
        monkeypatch.setattr(module, "store", storage)
        monkeypatch.setattr(module.settings, "mongodb_uri", "mongodb://test")
        monkeypatch.setattr(module.settings, "mongodb_database", "content_bot_test")
    monkeypatch.setattr(prune_mongodb, "local_api_is_running", lambda: False)
    monkeypatch.setattr(restore_mongodb, "local_api_is_running", lambda: False)


def test_mongodb_backup_restore_and_prune(mongo_store: MongoStore, tmp_path: Path, monkeypatch) -> None:
    configure_modules(mongo_store, monkeypatch)
    mongo_store.db.content_items.insert_one(
        {"_id": 1, "last_seen_at": datetime.now(UTC) - timedelta(days=100)}
    )
    backup = tmp_path / "content-bot.json"

    monkeypatch.setattr(sys, "argv", ["backup_mongodb.py", "--destination", str(backup)])
    assert backup_mongodb.main() == 0
    mongo_store.db.content_items.delete_many({})

    monkeypatch.setattr(
        sys,
        "argv",
        ["restore_mongodb.py", "--source", str(backup), "--replace-current"],
    )
    assert restore_mongodb.main() == 0
    assert mongo_store.db.content_items.find_one({"_id": 1}) is not None

    monkeypatch.setattr(sys, "argv", ["prune_mongodb.py", "--days", "90", "--apply"])
    assert prune_mongodb.main() == 0
    assert mongo_store.db.content_items.count_documents({}) == 0
