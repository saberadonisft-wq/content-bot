import asyncio

import pytest

from app.crawlers.adapters.tieba import (
    TiebaSearchAdapter,
    TiebaSearchCursor,
    TiebaThread,
    TiebaThreadPage,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)


def context() -> RunContext:
    return RunContext(
        run_id="tieba-run",
        keyword_id=1,
        source_id="tieba",
        provider_id="cbce_tieba",
        operation="search",
        target={"kind": "keyword"},
        terms=("game",),
        filters={},
        budgets=RunBudgets(max_items=2, max_requests=2, deadline_seconds=30),
    )


class FakeProvider:
    closed = False

    async def open(self, context, cancellation):
        return None

    async def search_page(self, terms, cursor, limit, cancellation):
        return TiebaThreadPage(
            (
                TiebaThread(
                    "1234567890",
                    "https://tieba.baidu.com/p/1234567890?pn=2",
                    " Public thread ",
                    body="body",
                    author_id="raw-user-id",
                    metrics={"comment_count": 12, "unknown": 99},
                ),
            ),
            None,
            False,
        )

    async def close(self):
        self.closed = True


def test_tieba_cursor_round_trip() -> None:
    cursor = TiebaSearchCursor(2, 3, 4)
    assert TiebaSearchCursor.decode(cursor.encode()) == cursor
    with pytest.raises(CrawlerFailure) as captured:
        TiebaSearchCursor.decode("invalid")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_tieba_adapter_normalizes_identity_privacy_and_metric() -> None:
    provider = FakeProvider()

    async def run():
        adapter = TiebaSearchAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 2)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    record = page.items[0]
    assert record.external_id == "1234567890"
    assert record.canonical_url == "https://tieba.baidu.com/p/1234567890"
    assert record.metrics == {"comment_count": 12}
    assert record.author_pseudonym.startswith("tieba_")
    assert "raw-user-id" not in repr(record)
    assert provider.closed is True
