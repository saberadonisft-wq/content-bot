import asyncio
import pytest

from app.services import http_pool


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

