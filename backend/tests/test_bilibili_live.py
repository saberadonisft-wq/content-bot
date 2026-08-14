"""Opt-in live smoke; never runs in the default regression suite."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from app.config import settings
from app.crawlers import SOURCE_REGISTRY
from app.crawlers.adapters.bilibili import (
    BilibiliDetailAdapter,
    BilibiliDomDetailProvider,
    BilibiliDomSearchProvider,
    BilibiliSearchAdapter,
)
from app.crawlers.runtime import (
    BrowserLaunchRequest,
    BrowserSession,
    CancellationToken,
    CrawlerSupervisor,
    IdentityPseudonymizer,
    PlaywrightPersistentDriver,
    ProfileNamespace,
    RunBudgets,
    RunContext,
)

pytestmark = pytest.mark.skipif(
    os.getenv("CBCE_LIVE_BILIBILI") != "1",
    reason="Set CBCE_LIVE_BILIBILI=1 for a bounded public-browser smoke test.",
)


def test_bilibili_public_dom_search_live_smoke() -> None:
    async def run():
        executable = (
            settings.content_bot_cbce_browser_executable_path
            or settings.content_bot_coccoc_executable_path
        )
        with tempfile.TemporaryDirectory(prefix="cbce-bili-live-test-") as temp:
            profile = ProfileNamespace(
                Path(temp) / "profiles-v2", SOURCE_REGISTRY
            ).profile("bilibili", "live-test")
            session = BrowserSession(
                PlaywrightPersistentDriver(),
                BrowserLaunchRequest(executable, profile),
                owner_id="bilibili-live-test",
            )
            adapter = BilibiliSearchAdapter(
                BilibiliDomSearchProvider(session),
                IdentityPseudonymizer(b"test-only-pseudonym-key-material-32"),
            )
            context = RunContext(
                run_id="bilibili-live-test",
                keyword_id=0,
                source_id="bilibili",
                provider_id="cbce_bilibili",
                operation="search",
                target={"kind": "keyword"},
                terms=("game",),
                filters={},
                budgets=RunBudgets(max_items=3, max_requests=2, deadline_seconds=60),
            )
            records = []
            cursors = []

            async def persist(record):
                records.append(record)

            async def commit(cursor):
                cursors.append(cursor)

            result = await CrawlerSupervisor().run_search(
                context,
                adapter,
                persist=persist,
                commit_cursor=commit,
                page_size=3,
            )
            detail_profile = ProfileNamespace(
                Path(temp) / "profiles-v2", SOURCE_REGISTRY
            ).profile("bilibili", "detail-live-test")
            detail_session = BrowserSession(
                PlaywrightPersistentDriver(),
                BrowserLaunchRequest(executable, detail_profile),
                owner_id="bilibili-detail-live-test",
            )
            detail_adapter = BilibiliDetailAdapter(
                BilibiliDomDetailProvider(detail_session),
                IdentityPseudonymizer(b"test-only-pseudonym-key-material-32"),
            )
            detail_context = RunContext(
                run_id="bilibili-detail-live-test",
                keyword_id=0,
                source_id="bilibili",
                provider_id="cbce_bilibili",
                operation="fetch_detail",
                target={"kind": "content_url", "url": records[0].canonical_url},
                terms=(),
                filters={},
                budgets=RunBudgets(max_items=1, max_requests=1, deadline_seconds=60),
            )
            await detail_adapter.open(detail_context, CancellationToken())
            try:
                detail = await detail_adapter.fetch()
            finally:
                await detail_adapter.close()
            return result, records, cursors, detail

    result, records, cursors, detail = asyncio.run(run())
    assert result.persisted_count == 3
    assert len(records) == 3
    assert all(record.source_id == "bilibili" for record in records)
    assert all(
        record.canonical_url.startswith("https://www.bilibili.com/video/")
        for record in records
    )
    assert cursors[-1].startswith("v1:")
    assert detail.external_id == records[0].external_id
    assert detail.title
    assert detail.body
    assert detail.author_pseudonym == ""
    assert set(detail.metrics) <= {
        "view_count",
        "like_count",
        "favorite_count",
    }
