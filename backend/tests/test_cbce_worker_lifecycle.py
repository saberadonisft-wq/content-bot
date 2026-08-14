import asyncio
from pathlib import Path

import pytest

from app.crawlers.runtime import (
    CancellationToken,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    Page,
    PseudonymKeyStore,
    WorkerEnvelope,
    WorkerMessageKind,
)
from app.crawlers.worker import (
    BilibiliWorkerRequest,
    TiebaWorkerRequest,
    run_bilibili_worker,
    run_tieba_worker,
)


def _start() -> WorkerEnvelope:
    return WorkerEnvelope(
        kind=WorkerMessageKind.START,
        sequence=0,
        run_id="run-1",
        source_run_id="source-run-1",
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="search",
        payload={},
    )


def _request(tmp_path: Path) -> BilibiliWorkerRequest:
    browser = tmp_path / "browser.exe"
    browser.touch()
    key = PseudonymKeyStore(tmp_path / "secrets")
    key.load_or_create()
    return BilibiliWorkerRequest(
        terms=("game",),
        max_items=1,
        max_requests=1,
        deadline_seconds=30,
        initial_cursor=None,
        browser_executable=browser,
        profile_root=tmp_path / "profiles-v2",
        pseudonym_key_ref=key.path,
    )


def _tieba_request(tmp_path: Path) -> TiebaWorkerRequest:
    request = _request(tmp_path)
    return TiebaWorkerRequest(
        terms=request.terms,
        max_items=request.max_items,
        max_requests=request.max_requests,
        deadline_seconds=request.deadline_seconds,
        initial_cursor=request.initial_cursor,
        browser_executable=request.browser_executable,
        profile_root=request.profile_root,
        pseudonym_key_ref=request.pseudonym_key_ref,
    )


def _tieba_start() -> WorkerEnvelope:
    return WorkerEnvelope(
        kind=WorkerMessageKind.START,
        sequence=0,
        run_id="tieba-run-1",
        source_run_id="tieba-source-run-1",
        source_id="tieba",
        provider_id="cbce_tieba",
        operation="search",
        payload={},
    )


class RecordingWriter:
    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.events = []

    def emit(self, kind, payload=None):
        if kind is WorkerMessageKind.COMPLETE:
            assert self.adapter.closed is True
        self.events.append((kind, payload or {}))


class SuccessfulAdapter:
    source_id = "bilibili"
    provider_id = "cbce_bilibili"
    owns_resources = True

    def __init__(self) -> None:
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def fetch_page(self, context, cursor, limit):
        return Page(
            (
                ContentRecord(
                    source_id="bilibili",
                    external_id="BV1",
                    canonical_url="https://www.bilibili.com/video/BV1",
                    title="Synthetic",
                ),
            ),
            None,
            False,
        )

    async def close(self):
        self.closed = True


class CancellingAdapter(SuccessfulAdapter):
    async def open(self, context, cancellation):
        await cancellation.wait()
        cancellation.raise_if_cancelled()


class SuccessfulTiebaAdapter(SuccessfulAdapter):
    source_id = "tieba"
    provider_id = "cbce_tieba"

    async def fetch_page(self, context, cursor, limit):
        return Page(
            (
                ContentRecord(
                    source_id="tieba",
                    external_id="1234567890",
                    canonical_url="https://tieba.baidu.com/p/1234567890",
                    title="Synthetic thread",
                ),
            ),
            None,
            False,
        )


def test_worker_closes_adapter_before_emitting_complete(monkeypatch, tmp_path) -> None:
    adapter = SuccessfulAdapter()
    monkeypatch.setattr(
        "app.crawlers.worker.BilibiliSearchAdapter",
        lambda provider, pseudonymizer: adapter,
    )
    writer = RecordingWriter(adapter)

    asyncio.run(
        run_bilibili_worker(_start(), _request(tmp_path), CancellationToken(), writer)
    )

    assert adapter.closed is True
    assert writer.events[-1][0] is WorkerMessageKind.COMPLETE


def test_worker_cancellation_closes_adapter_and_does_not_emit_complete(
    monkeypatch, tmp_path
) -> None:
    adapter = CancellingAdapter()
    monkeypatch.setattr(
        "app.crawlers.worker.BilibiliSearchAdapter",
        lambda provider, pseudonymizer: adapter,
    )
    writer = RecordingWriter(adapter)
    cancellation = CancellationToken()

    async def run():
        task = asyncio.create_task(
            run_bilibili_worker(_start(), _request(tmp_path), cancellation, writer)
        )
        await asyncio.sleep(0)
        cancellation.cancel()
        with pytest.raises(CrawlerFailure) as captured:
            await task
        assert captured.value.code is CrawlerErrorCode.CANCELLED

    asyncio.run(run())
    assert adapter.closed is True
    assert all(kind is not WorkerMessageKind.COMPLETE for kind, _ in writer.events)


def test_tieba_worker_reuses_shared_executor_and_closes_before_complete(
    monkeypatch, tmp_path
) -> None:
    adapter = SuccessfulTiebaAdapter()
    monkeypatch.setattr(
        "app.crawlers.worker.TiebaSearchAdapter",
        lambda provider, pseudonymizer: adapter,
    )
    writer = RecordingWriter(adapter)

    asyncio.run(
        run_tieba_worker(
            _tieba_start(), _tieba_request(tmp_path), CancellationToken(), writer
        )
    )

    assert adapter.closed is True
    assert writer.events[-1][0] is WorkerMessageKind.COMPLETE
