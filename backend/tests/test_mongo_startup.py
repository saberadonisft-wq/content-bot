from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main, mongo
from app.mongo import MongoStore


def test_default_store_does_not_connect_during_construction(monkeypatch) -> None:
    monkeypatch.setattr(mongo.settings, "mongodb_uri", "mongodb://example.invalid/content-bot")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("MongoClient must not be created while modules import")

    monkeypatch.setattr(mongo, "MongoClient", fail_if_called)
    storage = MongoStore()

    assert storage.is_available is False


def test_readiness_ping_is_cached() -> None:
    calls = 0

    class Admin:
        def command(self, name: str) -> dict:
            nonlocal calls
            assert name == "ping"
            calls += 1
            return {"ok": 1}

    database = SimpleNamespace(client=SimpleNamespace(admin=Admin()))
    storage = MongoStore(database)  # type: ignore[arg-type]

    assert storage.ping_cached(ttl_seconds=5) is True
    assert storage.ping_cached(ttl_seconds=5) is True
    assert calls == 1


def test_persistence_routes_do_not_have_a_mongodb_guard(mongo_store: MongoStore) -> None:
    with TestClient(main.app) as client:
        mongo_store._available = False
        mongo_store.client = None
        headers = {
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
        }

        preflight = client.options("/api/v1/keywords", headers=headers)
        response = client.get(
            "/api/v1/keywords",
            headers={"Origin": "http://127.0.0.1:5173"},
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_credential_update_cors_preflight_allows_put() -> None:
    with TestClient(main.app) as client:
        preflight = client.options(
            "/api/v1/credentials",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert preflight.status_code == 200
    assert "PUT" in preflight.headers["access-control-allow-methods"]
