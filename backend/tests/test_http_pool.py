import asyncio

import httpx
import pytest

from app.config import settings
from app.services import http_pool
from app.services.connectors import MastodonConnector, SearchQuery


def test_http_pool_reuses_client_for_same_key() -> None:
    async def run():
        await http_pool.close_http_pools()
        client1 = await http_pool.get_client(base_url="https://api.example.com")
        client2 = await http_pool.get_client(base_url="https://api.example.com")
        assert client1 is client2
        assert not client1.is_closed

        # Different base_url gets a different client
        client3 = await http_pool.get_client(base_url="https://other.example.com")
        assert client3 is not client1

        # Public client (no base url)
        pub1 = await http_pool.get_client()
        pub2 = await http_pool.get_client()
        assert pub1 is pub2
        assert pub1 is not client1

        # Cleanup
        await http_pool.close_http_pools()
        assert client1.is_closed
        assert client3.is_closed
        assert pub1.is_closed

    asyncio.run(run())


def test_pool_respects_headers_timeouts_redirects_and_extra_configuration():
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"query": str(request.url.query)}))
        try:
            one = await http_pool.get_client(timeout=1, headers={"Accept": "application/json"}, transport=transport)
            two = await http_pool.get_client(timeout=99, follow_redirects=True, headers={"Accept": "text/html"}, transport=transport)
            assert one is not two
            assert one.timeout.read == 1 and two.timeout.read == 99
            assert not one.follow_redirects and two.follow_redirects
            assert one.headers["accept"] == "application/json"
            assert two.headers["accept"] == "text/html"
            same = await http_pool.get_client(timeout=httpx.Timeout(1, connect=10), headers={"accept": "application/json"}, transport=transport)
            assert same is one
            with_params = await http_pool.get_client(params={"limit": 7}, transport=transport)
            response = await with_params.get("https://example.com")
            assert "limit=7" in response.json()["query"]
            first_token = await http_pool.get_client(headers={"Authorization": "Bearer first"}, transport=transport)
            rotated = await http_pool.get_client(headers={"Authorization": "Bearer second"}, transport=transport)
            assert first_token is not rotated
            assert rotated.headers["authorization"] == "Bearer second"
            with pytest.raises(TypeError):
                await http_pool.get_client(unsupported_option=True)
        finally:
            await http_pool.close_http_pools()

    asyncio.run(run())


def test_pool_constructs_once_under_concurrency_and_closes_all_clients():
    created = []

    def factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs)
        created.append(client)
        return client

    async def run():
        with http_pool.use_client_factory(factory):
            clients = await asyncio.gather(*(http_pool.get_client() for _ in range(20)))
            assert len(created) == 1
            assert all(client is clients[0] for client in clients)
            await http_pool.close_http_pools()
            assert clients[0].is_closed
            fresh = await http_pool.get_client()
            assert fresh is not clients[0]
            await http_pool.close_http_pools()
        assert all(client.is_closed for client in created)

    asyncio.run(run())


def test_pool_does_not_reuse_clients_across_event_loops():
    async def run():
        client = await http_pool.get_client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        await http_pool.close_http_pools()
        return client

    assert asyncio.run(run()) is not asyncio.run(run())


def test_mastodon_health_and_search_use_production_pool_with_mock_transport(monkeypatch):
    monkeypatch.setattr(settings, "mastodon_instances", "mastodon.social")
    requests = []
    created = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={} if request.url.path == "/api/v2/instance" else [])

    def factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        created.append(client)
        return client

    async def run():
        with http_pool.use_client_factory(factory):
            try:
                connector = MastodonConnector()
                assert (await connector.deep_healthcheck()).state == "ready"
                assert [item async for item in connector.search(SearchQuery(1, "Game", [], 1))] == []
                await connector.deep_healthcheck()
                assert len(created) == 2
                assert [request.extensions["timeout"]["read"] for request in requests] == [10, 30, 10]
            finally:
                await http_pool.close_http_pools()
        assert all(client.is_closed for client in created)

    asyncio.run(run())
