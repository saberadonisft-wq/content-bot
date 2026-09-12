from __future__ import annotations

import asyncio
import os
import tempfile
from importlib.machinery import ModuleSpec

import mongomock
import pytest

# Test-created application services must never recover jobs from the desktop app.
_application_test_data = tempfile.TemporaryDirectory(prefix="content-bot-tests-")
os.environ["CONTENT_BOT_DATA_DIR"] = _application_test_data.name

from app import main
from app.application_services import AppServices
from app.mongo import MongoStore


@pytest.fixture
def available_browser_dependency(monkeypatch):
    """Fake-worker tests declare package availability without installing a browser."""
    from app.services import cbce_runtime

    original = cbce_runtime.find_spec
    monkeypatch.setattr(
        cbce_runtime,
        "find_spec",
        lambda name: (
            ModuleSpec(name, loader=None) if name == "playwright" else original(name)
        ),
    )


@pytest.fixture
def mongo_store() -> MongoStore:
    client = mongomock.MongoClient(tz_aware=True)
    storage = MongoStore(client["content_bot_test"])
    storage.initialize(ping=False)
    return storage


@pytest.fixture
def application_services(mongo_store):
    services = AppServices.create(storage=mongo_store)
    yield services
    if not services._closed:
        asyncio.run(services.shutdown())


@pytest.fixture(autouse=True)
def isolate_application_store(
    application_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use explicit app resources; construction/import no longer owns managers."""
    monkeypatch.setattr(
        main.app.state, "services_factory", lambda: application_services
    )
    monkeypatch.setattr(main.app.state, "services", application_services)
