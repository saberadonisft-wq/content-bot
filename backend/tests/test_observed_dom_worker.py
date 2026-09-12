from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import settings
from app.crawlers.observed_dom_contract import DOM_CONTRACT_SCHEMA
from app.crawlers.runtime import (
    CancellationToken,
    ContentRecord,
    Page,
    PseudonymKeyStore,
    WorkerEnvelope,
    WorkerMessageKind,
    WorkerProcessResult,
)
from app.crawlers.worker import (
    ObservedDomWorkerRequest,
    _request_from_payload,
    run_observed_dom_worker,
)
from app.services.cbce_connectors import CbceObservedDomConnector
from app.services.connectors import SearchQuery, default_connectors

pytestmark = pytest.mark.usefixtures("available_browser_dependency")


def _contract(root: Path, source_id: str = "xhs") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": DOM_CONTRACT_SCHEMA,
        "source_id": source_id,
        "provider_id": f"cbce_{source_id}",
        "mode": "browser_video_v1",
        "observed_at": datetime(2026, 8, 13, tzinfo=UTC).isoformat(),
        "evidence_digest": "a" * 64,
        "reviewed_by": "test-reviewer",
        "search_url_template": (
            "https://www.xiaohongshu.com/search_result?keyword={query}&page={page}"
        ),
        "login_selectors": ["div.login-panel"],
        "selectors": {
            "root_selector": "main.results",
            "card_selector": "article.note",
            "link_selector": "a.note",
            "title_selector": "h2.title",
            "next_selector": "button.next",
            "metric_selectors": {"like_count": "span.likes"},
        },
    }
    path = root / f"{source_id}.search.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _worker_payload(tmp_path: Path) -> dict:
    return {
        "action": "xhs_search",
        "terms": ["game", "game"],
        "max_items": 3,
        "max_requests": 2,
        "deadline_seconds": 30,
        "initial_cursor": None,
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles"),
        "pseudonym_key_ref": str(tmp_path / "pseudonym.key"),
        "contract_root": str(tmp_path / "contracts"),
    }


def test_observed_dom_worker_request_is_source_bound_and_bounded(tmp_path: Path) -> None:
    request = _request_from_payload(_worker_payload(tmp_path))

    assert isinstance(request, ObservedDomWorkerRequest)
    assert request.source_id == "xhs"
    assert request.terms == ("game",)
    assert request.max_requests == 2
    assert request.contract_root == (tmp_path / "contracts").resolve()


class FakeProcessSupervisor:
    def __init__(self) -> None:
        self.start = None

    async def execute(self, spec, start, *, on_message, control=None):
        del spec, control
        self.start = start
        identity = {
            "run_id": start.run_id,
            "source_run_id": start.source_run_id,
            "source_id": start.source_id,
            "provider_id": start.provider_id,
            "operation": start.operation,
        }
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.COMPLETE,
                sequence=1,
                payload={"item_count": 0, "request_count": 0, "cursor": None},
                **identity,
            )
        )
        return WorkerProcessResult(WorkerMessageKind.COMPLETE, 0, 1, "")


def test_observed_connector_requires_contract_and_forwards_tiny_budgets(
    monkeypatch, tmp_path: Path
) -> None:
    contract_root = tmp_path / "contracts"
    _contract(contract_root)
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(settings, "content_bot_cbce_contract_root", contract_root)
    monkeypatch.setattr(settings, "content_bot_cbce_browser_executable_path", executable)
    monkeypatch.setattr(settings, "content_bot_cbce_profile_root", tmp_path / "profiles")
    monkeypatch.setattr(settings, "mediacrawler_profile_dir", tmp_path / "legacy")
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    supervisor = FakeProcessSupervisor()
    connector = CbceObservedDomConnector("xhs", supervisor)
    query = SearchQuery(
        1,
        "game",
        [],
        3,
        request_budget=2,
        deadline_seconds=4,
    )

    async def run():
        status = await connector.healthcheck()
        items = [item async for item in connector.search(query)]
        return status, items

    status, items = asyncio.run(run())
    assert status.state == "ready"
    assert items == []
    assert supervisor.start.payload["action"] == "xhs_search"
    assert supervisor.start.payload["max_requests"] == 2
    assert supervisor.start.payload["deadline_seconds"] == 4
    assert supervisor.start.payload["contract_root"] == str(contract_root)


def test_default_connector_switches_only_with_explicit_provider_override(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"xhs":{"search":"cbce_xhs"}}',
    )

    connectors = default_connectors()

    assert isinstance(connectors["xhs"], CbceObservedDomConnector)
    assert not isinstance(connectors["douyin"], CbceObservedDomConnector)


class SuccessfulAdapter:
    source_id = "xhs"
    provider_id = "cbce_xhs"
    owns_resources = True

    def __init__(self) -> None:
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def fetch_page(self, context, cursor, limit):
        return Page(
            (
                ContentRecord(
                    source_id="xhs",
                    external_id="0123456789abcdef01234567",
                    canonical_url=(
                        "https://www.xiaohongshu.com/explore/"
                        "0123456789abcdef01234567"
                    ),
                    title="Synthetic reviewed-contract record",
                ),
            ),
            None,
            False,
        )

    async def close(self):
        self.closed = True


class RecordingWriter:
    def __init__(self, adapter: SuccessfulAdapter) -> None:
        self.adapter = adapter
        self.events = []

    def emit(self, kind, payload=None):
        if kind is WorkerMessageKind.COMPLETE:
            assert self.adapter.closed is True
        self.events.append((kind, payload or {}))


def test_observed_worker_uses_shared_lifecycle_and_closes_before_complete(
    monkeypatch, tmp_path: Path
) -> None:
    contract_root = tmp_path / "contracts"
    _contract(contract_root)
    browser = tmp_path / "browser.exe"
    browser.touch()
    key = PseudonymKeyStore(tmp_path / "secrets")
    key.load_or_create()
    request = ObservedDomWorkerRequest(
        source_id="xhs",
        terms=("game",),
        max_items=1,
        max_requests=1,
        deadline_seconds=30,
        initial_cursor=None,
        browser_executable=browser,
        profile_root=tmp_path / "profiles",
        pseudonym_key_ref=key.path,
        contract_root=contract_root,
    )
    start = WorkerEnvelope(
        kind=WorkerMessageKind.START,
        sequence=0,
        run_id="run-xhs",
        source_run_id="source-run-xhs",
        source_id="xhs",
        provider_id="cbce_xhs",
        operation="search",
        payload={},
    )
    adapter = SuccessfulAdapter()
    monkeypatch.setattr(
        "app.crawlers.worker.BrowserVideoSearchAdapter",
        lambda **kwargs: adapter,
    )
    writer = RecordingWriter(adapter)

    asyncio.run(
        run_observed_dom_worker(start, request, CancellationToken(), writer)
    )

    assert writer.events[-1][0] is WorkerMessageKind.COMPLETE
    assert adapter.closed is True
