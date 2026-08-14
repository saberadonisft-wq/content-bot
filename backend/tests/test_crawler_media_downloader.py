import asyncio
import hashlib

import httpx
import pytest

from app.crawlers.runtime import (
    BoundedMediaDownloader,
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    MediaDownloadPolicy,
)


class Response:
    def __init__(self, body=b"", *, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body
        self.chunks = chunks
        self.closed = False

    async def aiter_bytes(self, _size):
        values = self.chunks if self.chunks is not None else [self.body]
        for value in values:
            yield value

    async def aclose(self):
        self.closed = True


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def build_request(self, method, url):
        self.requests.append((method, url))
        return httpx.Request(method, url)

    async def send(self, request, *, stream):
        assert stream is True
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def policy(**overrides):
    values = {
        "source_id": "bluesky",
        "allowed_hosts": ("cdn.example",),
        "max_files": 2,
        "max_total_bytes": 20,
        "max_file_bytes": 12,
    }
    values.update(overrides)
    return MediaDownloadPolicy(**values)


async def download(tmp_path, client, url="https://cdn.example/image?signature=secret", **overrides):
    async with BoundedMediaDownloader(
        tmp_path,
        policy(**overrides),
        client=client,
    ) as downloader:
        return await downloader.download(url, cancellation=CancellationToken())


def test_media_download_is_atomic_hashed_and_drops_query_from_artifact(tmp_path) -> None:
    response = Response(
        b"image-bytes",
        headers={"Content-Type": "image/jpeg", "Content-Length": "11"},
    )
    artifact = asyncio.run(download(tmp_path, Client(response)))
    assert artifact.byte_count == 11
    assert artifact.sha256 == hashlib.sha256(b"image-bytes").hexdigest()
    assert artifact.path.read_bytes() == b"image-bytes"
    assert artifact.path.suffix == ".jpg"
    assert artifact.path.parent == (tmp_path / "bluesky").resolve()
    assert artifact.source_url == "https://cdn.example/image"
    assert "secret" not in repr(artifact)
    assert not list(artifact.path.parent.glob("*.part"))
    assert response.closed is True


def test_media_redirect_must_stay_on_allowlisted_https_host(tmp_path) -> None:
    redirect = Response(status=302, headers={"Location": "https://evil.example/private"})
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(download(tmp_path, Client(redirect)))
    assert raised.value.code is CrawlerErrorCode.PERMISSION_REQUIRED
    assert redirect.closed is True
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/image",
        "https://user:secret@cdn.example/image",
        "https://cdn.example:444/image",
        "https://127.0.0.1/image",
        "file:///tmp/image",
    ],
)
def test_media_url_boundary_rejects_unsafe_targets(tmp_path, url) -> None:
    with pytest.raises((CrawlerFailure, ValueError)):
        asyncio.run(download(tmp_path, Client(), url=url))


def test_content_length_and_streaming_caps_fail_without_partial_file(tmp_path) -> None:
    declared = Response(
        b"ignored",
        headers={"Content-Type": "video/mp4", "Content-Length": "13"},
    )
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(download(tmp_path, Client(declared)))
    assert raised.value.code is CrawlerErrorCode.BUDGET_EXHAUSTED

    streamed = Response(
        headers={"Content-Type": "image/png"},
        chunks=[b"123456", b"7890123"],
    )
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(download(tmp_path, Client(streamed)))
    assert raised.value.code is CrawlerErrorCode.BUDGET_EXHAUSTED
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.png"))


def test_media_type_and_file_budget_are_enforced(tmp_path) -> None:
    async def run():
        client = Client(
            Response(b"html", headers={"Content-Type": "text/html"}),
            Response(b"one", headers={"Content-Type": "image/png"}),
            Response(b"two", headers={"Content-Type": "image/png"}),
        )
        async with BoundedMediaDownloader(
            tmp_path,
            policy(max_files=1),
            client=client,
        ) as downloader:
            with pytest.raises(CrawlerFailure) as unsupported:
                await downloader.download(
                    "https://cdn.example/page", cancellation=CancellationToken()
                )
            assert unsupported.value.code is CrawlerErrorCode.UNSUPPORTED
            await downloader.download(
                "https://cdn.example/one", cancellation=CancellationToken()
            )
            with pytest.raises(CrawlerFailure) as exhausted:
                await downloader.download(
                    "https://cdn.example/two", cancellation=CancellationToken()
                )
            assert exhausted.value.code is CrawlerErrorCode.BUDGET_EXHAUSTED

    asyncio.run(run())


def test_duplicate_url_and_content_hash_do_not_create_duplicate_files(tmp_path) -> None:
    async def run():
        client = Client(
            Response(b"same", headers={"Content-Type": "image/png"}),
            Response(b"same", headers={"Content-Type": "image/png"}),
        )
        async with BoundedMediaDownloader(
            tmp_path,
            policy(max_files=2),
            client=client,
        ) as downloader:
            first = await downloader.download(
                "https://cdn.example/one?token=first",
                cancellation=CancellationToken(),
            )
            cached = await downloader.download(
                "https://cdn.example/one?token=first",
                cancellation=CancellationToken(),
            )
            duplicate = await downloader.download(
                "https://cdn.example/two?token=second",
                cancellation=CancellationToken(),
            )
            return client, first, cached, duplicate

    client, first, cached, duplicate = asyncio.run(run())
    assert first == cached
    assert duplicate.sha256 == first.sha256
    assert duplicate.path == first.path
    assert len(client.requests) == 2
    assert len(list((tmp_path / "bluesky").glob("*.png"))) == 1
    assert "token" not in repr(duplicate)


def test_active_content_media_format_is_rejected(tmp_path) -> None:
    response = Response(b"<svg/>", headers={"Content-Type": "image/svg+xml"})
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(download(tmp_path, Client(response)))
    assert raised.value.code is CrawlerErrorCode.UNSUPPORTED


def test_cancellation_and_transport_retry_leave_no_partial_file(monkeypatch, tmp_path) -> None:
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(
        "app.crawlers.runtime.media_downloader.asyncio.sleep", no_sleep
    )
    request = httpx.Request("GET", "https://cdn.example/image")
    client = Client(
        httpx.ConnectError("private transport detail", request=request),
        Response(b"ok", headers={"Content-Type": "image/png"}),
    )
    artifact = asyncio.run(download(tmp_path, client, url="https://cdn.example/image"))
    assert artifact.path.read_bytes() == b"ok"
    assert len(client.requests) == 2

    token = CancellationToken()
    token.cancel()

    async def cancelled():
        async with BoundedMediaDownloader(
            tmp_path,
            policy(),
            client=Client(Response(b"never", headers={"Content-Type": "image/png"})),
        ) as downloader:
            await downloader.download("https://cdn.example/cancel", cancellation=token)

    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(cancelled())
    assert raised.value.code is CrawlerErrorCode.CANCELLED
    assert "private transport detail" not in raised.value.safe_message
