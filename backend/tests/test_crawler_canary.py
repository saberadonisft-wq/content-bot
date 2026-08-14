from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.crawlers.contracts import Operation
from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.connectors import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from app.services.crawler_canary import (
    CanaryRequest,
    CanaryState,
    CrawlerCanaryRunner,
)
from app.services.steam_reviews import SteamRequestBudget
from app.services.youtube_api import YouTubeQuotaBudget


class FakeConnector(SourceConnector):
    source_id = "youtube"
    label = "Fake"
    group = "test"
    capabilities = ConnectorCapabilities(True)

    def __init__(
        self,
        *,
        failure: CrawlerFailure | None = None,
        delay: float = 0,
    ) -> None:
        self.failure = failure
        self.delay = delay
        self.received_query: SearchQuery | None = None
        self.calls = 0

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "fake")

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict | None = None,
    ) -> AsyncIterator[RawContentItem]:
        del checkpoint
        self.calls += 1
        self.received_query = query
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        for index in range(10):
            yield RawContentItem(
                external_id=f"provider-secret-id-{index}",
                canonical_url=f"https://example.invalid/private/{index}",
                title="private title",
                body_snippet="private body",
                author="private author",
                published_at=datetime(2026, 1, index + 1, tzinfo=UTC),
                metrics={"view_count": index},
                raw_payload={"access_token": "never-report-this"},
            )


@pytest.mark.asyncio
async def test_canary_is_bounded_and_report_is_aggregate_only() -> None:
    connector = FakeConnector()
    runner = CrawlerCanaryRunner(connectors={"youtube": connector})
    secret_query = "unreleased-game-secret"

    report = await runner.run(
        CanaryRequest(
            source_id="youtube",
            operation=Operation.SEARCH,
            query=secret_query,
            max_items=2,
            max_requests=1,
            deadline_seconds=5,
        )
    )

    assert report.state is CanaryState.PASSED
    assert report.items_observed == 2
    assert report.unique_items_observed == 2
    assert report.metric_keys == ("view_count",)
    assert connector.received_query is not None
    assert connector.received_query.request_budget == 1
    assert connector.received_query.deadline_seconds == 5
    serialized = json.dumps(report.as_dict())
    for forbidden in (
        secret_query,
        "provider-secret-id",
        "example.invalid",
        "private title",
        "private body",
        "private author",
        "never-report-this",
        "raw_payload",
        "canonical_url",
    ):
        assert forbidden not in serialized
    assert report.as_dict()["persisted"] is False


@pytest.mark.asyncio
async def test_canary_preserves_typed_parser_drift_without_details() -> None:
    connector = FakeConnector(
        failure=CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Provider contract changed.",
            details={"access_token": "secret-sentinel"},
        )
    )
    report = await CrawlerCanaryRunner(
        connectors={"youtube": connector}
    ).run(
        CanaryRequest(
            source_id="youtube",
            operation=Operation.SEARCH,
            query="game",
        )
    )

    assert report.state is CanaryState.FAILED
    assert report.error_code == CrawlerErrorCode.PARSE_CHANGED.value
    serialized = json.dumps(report.as_dict())
    assert "secret-sentinel" not in serialized
    assert "access_token" not in serialized


@pytest.mark.asyncio
async def test_canary_enforces_outer_deadline() -> None:
    connector = FakeConnector(delay=2)
    report = await CrawlerCanaryRunner(
        connectors={"youtube": connector}
    ).run(
        CanaryRequest(
            source_id="youtube",
            operation=Operation.SEARCH,
            query="game",
            deadline_seconds=1,
        )
    )

    assert report.state is CanaryState.TIMED_OUT
    assert report.error_code == CrawlerErrorCode.DEADLINE_EXCEEDED.value
    assert report.items_observed == 0


@pytest.mark.asyncio
async def test_legacy_provider_requires_explicit_second_opt_in() -> None:
    connector = FakeConnector()
    connector.source_id = "xhs"
    report = await CrawlerCanaryRunner(connectors={"xhs": connector}).run(
        CanaryRequest(
            source_id="xhs",
            operation=Operation.SEARCH,
            query="game",
        )
    )

    assert report.state is CanaryState.SKIPPED
    assert report.error_code == "LEGACY_PROVIDER_REQUIRES_OPT_IN"
    assert connector.calls == 0


def test_canary_request_rejects_unsafe_shape_and_large_budget() -> None:
    with pytest.raises(ValueError, match="unsupported fields"):
        CanaryRequest.from_mapping(
            {
                "source_id": "youtube",
                "operation": "search",
                "query": "game",
                "access_token": "must-not-be-accepted",
            }
        )
    with pytest.raises(ValueError, match="max_requests"):
        CanaryRequest(
            source_id="youtube",
            operation=Operation.SEARCH,
            query="game",
            max_requests=11,
        )


def test_total_request_budgets_stop_across_buckets() -> None:
    youtube = YouTubeQuotaBudget(5, 5, max_total_requests=2)
    assert youtube.spend("/search")
    assert youtube.spend("/videos")
    assert not youtube.spend("/videos")

    steam = SteamRequestBudget(5, 5, total_limit=2)
    assert steam.spend_discovery()
    assert steam.spend_review()
    assert not steam.spend_review()


def test_cli_refuses_network_without_live_acknowledgement() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/crawler_canary.py"],
        cwd=backend_root,
        input="{}",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "without --live" in result.stderr
