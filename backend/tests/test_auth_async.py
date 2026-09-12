import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException

from app import main
from app.config import settings
from app.middleware import auth
from app.services.http_pool import close_http_pools, use_client_factory


def test_key_fetch_is_async_single_flight_and_expires(monkeypatch):
    monkeypatch.setattr(settings, "content_bot_auth_public_key", "")
    monkeypatch.setattr(settings, "content_bot_auth_server_url", "https://auth.example")
    now = [0]
    monkeypatch.setattr(auth, "monotonic", lambda: now[0])

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def respond(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"public_key": "test-public-key"})

        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)

        with use_client_factory(factory):
            try:
                pending = asyncio.gather(*(auth.get_public_key() for _ in range(20)))
                await asyncio.wait_for(entered.wait(), 1)
                # This coroutine executes while the network request is pending.
                assert not pending.done() and len(calls) == 1
                release.set()
                assert await pending == ["test-public-key"] * 20
                assert len(calls) == 1
                now[0] = 301
                assert await auth.get_public_key() == "test-public-key"
                assert len(calls) == 2
                monkeypatch.setattr(settings, "content_bot_auth_server_url", "https://changed.example")
                await auth.get_public_key()
                assert calls[-1].url.host == "changed.example"
                assert len(calls) == 3
            finally:
                await close_http_pools()

    asyncio.run(run())


def test_failed_key_fetch_has_bounded_retries(monkeypatch):
    monkeypatch.setattr(settings, "content_bot_auth_public_key", "")
    monkeypatch.setattr(settings, "content_bot_auth_server_url", "https://auth.example")
    calls = []
    now = [0]
    monkeypatch.setattr(auth, "monotonic", lambda: now[0])

    def respond(request):
        calls.append(request)
        return httpx.Response(503)

    async def run():
        with use_client_factory(lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)):
            try:
                errors = await asyncio.gather(*(auth.get_public_key() for _ in range(10)), return_exceptions=True)
                assert all(isinstance(error, HTTPException) and error.status_code == 503 for error in errors)
                assert len(calls) == 1
                now[0] = 2
                await asyncio.gather(auth.get_public_key(), return_exceptions=True)
                assert len(calls) == 2
            finally:
                await close_http_pools()

    asyncio.run(run())


def test_middleware_and_dependency_verify_once_and_reject_expired_stream_token(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(settings, "content_bot_auth_enabled", True)
    monkeypatch.setattr(settings, "content_bot_auth_public_key", public)
    app = FastAPI()
    app.middleware("http")(main.verify_auth_token_middleware)

    @app.get("/api/v1/runs/test/events")
    def protected(user=Depends(auth.get_current_user)):
        return {"user": user["sub"]}

    calls = []
    original = auth.verify_token

    async def verify(token):
        calls.append(token)
        return await original(token)

    monkeypatch.setattr(auth, "verify_token", verify)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {"sub": "test", "status": "approved", "exp": datetime.now(UTC) + timedelta(hours=1)}
            token = jwt.encode(payload, key, algorithm="RS256")
            assert (await client.get("/api/v1/runs/test/events", headers={"Authorization": f"Bearer {token}"})).status_code == 200
            assert len(calls) == 1
            expired = jwt.encode({**payload, "exp": datetime.now(UTC) - timedelta(seconds=1)}, key, algorithm="RS256")
            response = await client.get("/api/v1/runs/test/events", headers={"Authorization": f"Bearer {expired}"})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert (await client.get("/api/v1/runs/test/events")).status_code == 401

    asyncio.run(run())
