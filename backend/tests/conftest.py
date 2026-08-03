from __future__ import annotations

import mongomock
import pytest

from app import main
from app.mongo import MongoStore


@pytest.fixture
def mongo_store() -> MongoStore:
    client = mongomock.MongoClient(tz_aware=True)
    storage = MongoStore(client["content_bot_test"])
    storage.initialize(ping=False)
    return storage


@pytest.fixture(autouse=True)
def isolate_application_store(mongo_store: MongoStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep API/lifespan tests away from the developer's real MongoDB database."""
    monkeypatch.setattr(main, "store", mongo_store)
    main.run_manager.store = mongo_store
