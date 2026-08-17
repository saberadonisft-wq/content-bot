from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import settings
from app.crawlers.adapters.weibo.provider import normalize_weibo_post
from app.crawlers.licensed.mediacrawler.facade import LicensedSearchFacade
from app.crawlers.licensed.mediacrawler.policy import (
    LicensedReuseError,
    assert_licensed_reuse_allowed,
)
from app.crawlers.licensed.mediacrawler.source_map import (
    SourceMapError,
    load_source_map,
)
from app.crawlers.licensed.mediacrawler.weibo_api import (
    LicensedWeiboApiSearchProvider,
    _post_from_card,
    _result_cards,
    _search_url,
)
from app.crawlers.licensed.mediacrawler.weibo_field import SearchType
from app.crawlers.runtime import (
    CancellationToken,
    ContentRecord,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
    WorkerEnvelope,
    WorkerMessageKind,
)
from app.crawlers.runtime.errors import CrawlerErrorCode, CrawlerFailure
from app.crawlers.runtime.paginator import Page
from app.crawlers.worker import (
    ObservedDomWorkerRequest,
    _observed_dom_search_context,
)
from app.services.cbce_runtime import cbce_provider_rollout_status
from app.services.connectors import SearchQuery, default_connectors

LICENSED_ROOT = Path(__file__).resolve().parents[1] / "app/crawlers/licensed/mediacrawler"


def _context(*, provider_id: str = "licensed_weibo", filters: dict[str, object] | None = None) -> RunContext:
    return RunContext(
        run_id="licensed-test",
        keyword_id=0,
        source_id="weibo",
        provider_id=provider_id,
        operation="search",
        target={"kind": "keyword"},
        terms=("test",),
        filters=filters or {},
        budgets=RunBudgets(max_items=2, max_requests=2, deadline_seconds=30),
        started_at=datetime.now(UTC),
    )


def test_licensed_source_map_contains_notice_and_weibo_entry() -> None:
    source_map = load_source_map(LICENSED_ROOT)
    assert source_map.payload["upstream"]["commit"] == "071c8c0acaece3e82f2532cffb19faeddc9ec1c3"
    assert source_map.entries[0]["upstream_path"] == "media_platform/weibo/field.py"
    assert len(source_map.entries[0]["sha256"]) == 64


def test_licensed_source_map_rejects_modified_derived_file(tmp_path: Path) -> None:
    copied = tmp_path / "licensed"
    shutil.copytree(
        LICENSED_ROOT,
        copied,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (copied / "weibo_field.py").write_text("# modified\n", encoding="utf-8")
    with pytest.raises(SourceMapError, match="digest does not match"):
        load_source_map(copied)


def test_licensed_source_map_rejects_entry_commit_drift(tmp_path: Path) -> None:
    copied = tmp_path / "licensed"
    shutil.copytree(
        LICENSED_ROOT,
        copied,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    map_path = copied / "SOURCE_MAP.json"
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    payload["entries"][0]["source_commit"] = "f" * 40
    map_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceMapError, match="commit does not match"):
        load_source_map(copied)


def test_licensed_policy_rejects_broader_runtime_scope() -> None:
    assert_licensed_reuse_allowed()
    with pytest.raises(LicensedReuseError):
        assert_licensed_reuse_allowed({"allow_proxy_rotation": True})
    with pytest.raises(LicensedReuseError):
        assert_licensed_reuse_allowed({"non_commercial_learning": False})


def test_licensed_policy_caps_learning_run_size() -> None:
    policy = assert_licensed_reuse_allowed()
    with pytest.raises(LicensedReuseError):
        policy.validate_budgets(max_items=101, max_requests=1)


class _Delegate:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        cancellation.raise_if_cancelled()
        self.opened = True

    async def fetch_page(self, context: RunContext, cursor: object | None, limit: int) -> Page[ContentRecord, str]:
        item = ContentRecord(
            source_id="weibo",
            external_id="1",
            canonical_url="https://weibo.com/1",
            title="one",
        )
        second = ContentRecord(
            source_id="weibo",
            external_id="2",
            canonical_url="https://weibo.com/2",
            title="two",
        )
        return Page((item, second), "next", True)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_facade_validates_scope_and_clamps_delegate_page() -> None:
    delegate = _Delegate()
    facade = LicensedSearchFacade(
        source_id="weibo",
        provider_id="licensed_weibo",
        delegate=delegate,
        reuse_root=LICENSED_ROOT,
    )
    context = _context(filters={"search_type": "popular"})
    await facade.open(context, CancellationToken())
    page = await facade.fetch_page(context, None, 1)
    assert len(page.items) == 1
    assert page.next_cursor == "next"
    await facade.close()
    assert delegate.opened is True
    assert delegate.closed is True


@pytest.mark.asyncio
async def test_facade_rejects_wrong_provider_identity() -> None:
    facade = LicensedSearchFacade(
        source_id="weibo",
        provider_id="licensed_weibo",
        delegate=_Delegate(),
        reuse_root=LICENSED_ROOT,
    )
    with pytest.raises(ValueError, match="identity"):
        await facade.open(_context(provider_id="cbce_weibo"), CancellationToken())


def test_licensed_weibo_is_explicitly_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"weibo":{"search":"licensed_weibo"}}',
    )
    connector = default_connectors()["weibo"]
    assert type(connector).__name__ == "CbceLicensedWeiboConnector"
    assert connector.spec.provider_id == "licensed_weibo"
    assert connector.spec.action == "licensed_weibo_search"


@pytest.mark.asyncio
async def test_licensed_weibo_policy_failure_is_a_health_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"weibo":{"search":"licensed_weibo"}}',
    )
    monkeypatch.setattr(
        settings,
        "content_bot_licensed_reuse_noncommercial_only",
        False,
    )
    connector = default_connectors()["weibo"]
    assert connector.configured is False
    health = await connector.healthcheck()
    assert health.state == "disabled_by_policy"
    assert health.reason_code == "LICENSED_REUSE_POLICY"
    rollout = cbce_provider_rollout_status(
        "weibo",
        "licensed_weibo",
        "search",
    )
    assert rollout and rollout["ready"] is False
    assert rollout["reason_code"] == "LICENSED_REUSE_POLICY"


def test_licensed_weibo_connector_caps_worker_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"weibo":{"search":"licensed_weibo"}}',
    )
    connector = default_connectors()["weibo"]
    query = SearchQuery(0, "game", [], 500)
    assert connector._worker_budgets(query) == (100, 100)


class _ErrorSupervisor:
    async def execute(self, _spec, start_message, *, on_message, control=None):
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.ERROR,
                sequence=1,
                run_id=start_message.run_id,
                source_run_id=start_message.source_run_id,
                source_id=start_message.source_id,
                provider_id=start_message.provider_id,
                operation=start_message.operation,
                payload={
                    "code": CrawlerErrorCode.AUTH_REQUIRED.value,
                    "message": "login required",
                },
            )
        )


@pytest.mark.asyncio
async def test_cbce_search_propagates_typed_worker_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"weibo":{"search":"licensed_weibo"}}',
    )
    monkeypatch.setattr(
        "app.services.cbce_connectors.cbce_browser_preflight",
        lambda: {"ready": True},
    )
    connector = default_connectors()["weibo"]
    connector.process_supervisor = _ErrorSupervisor()
    with pytest.raises(CrawlerFailure) as captured:
        async for _item in connector.search(
            SearchQuery(0, "game", [], 1, request_budget=1)
        ):
            pass
    assert captured.value.code is CrawlerErrorCode.AUTH_REQUIRED


def test_licensed_worker_context_keeps_selected_provider_identity() -> None:
    request = ObservedDomWorkerRequest(
        source_id="weibo",
        terms=("game",),
        max_items=100,
        max_requests=100,
        deadline_seconds=30,
        initial_cursor=None,
        browser_executable=Path("browser.exe"),
        profile_root=Path("profiles"),
        pseudonym_key_ref=Path("identity.key"),
        contract_root=Path("contracts"),
        provider_id="licensed_weibo",
    )
    start = WorkerEnvelope(
        kind=WorkerMessageKind.START,
        sequence=0,
        run_id="run",
        source_run_id="source-run",
        source_id="weibo",
        provider_id="licensed_weibo",
        operation="search",
    )
    context = _observed_dom_search_context(
        start,
        request,
        provider_id=request.provider_id,
        contract_digest=None,
    )
    assert context.provider_id == "licensed_weibo"
    assert context.filters["coverage"] == "licensed_mobile_api"
    assert context.budgets.max_items == 100


class _BodyLocator:
    def __init__(self, body: str) -> None:
        self.body = body

    async def inner_text(self) -> str:
        return self.body


class _JsonPage:
    def __init__(self, body: str) -> None:
        self.body = body

    def locator(self, _selector: str) -> _BodyLocator:
        return _BodyLocator(self.body)


class _BrowserPage:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload)
        self.urls: list[str] = []

    async def open(
        self,
        _context: RunContext,
        _cancellation: CancellationToken,
    ) -> None:
        return None

    async def navigate(self, url: str, *_args, **_kwargs) -> _JsonPage:
        self.urls.append(url)
        return _JsonPage(self.body)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_licensed_weibo_terminal_page_stops_without_extra_cursor() -> None:
    browser = _BrowserPage(
        {
            "ok": 1,
            "data": {
                "cards": [
                    {
                        "card_type": 9,
                        "mblog": {"id": "B1", "text": "one"},
                    }
                ],
                "cardlistInfo": {"has_more": "0"},
            },
        }
    )
    provider = LicensedWeiboApiSearchProvider(browser)
    context = _context(filters={"search_type": "popular"})
    await provider.open(context, CancellationToken())
    page = await provider.search_page(
        context.terms,
        None,
        10,
        CancellationToken(),
    )
    assert len(page.items) == 1
    assert page.has_more is False
    assert page.next_cursor is None
    assert browser.urls[0] == "https://passport.weibo.com"
    assert "100103type%3D60" in browser.urls[-1]


@pytest.mark.asyncio
async def test_licensed_weibo_terminal_page_advances_to_next_term() -> None:
    browser = _BrowserPage(
        {
            "ok": 1,
            "data": {"cards": [], "cardlistInfo": {"has_more": False}},
        }
    )
    provider = LicensedWeiboApiSearchProvider(browser)
    context = replace(_context(), terms=("first", "second"))
    await provider.open(context, CancellationToken())
    page = await provider.search_page(
        context.terms,
        None,
        10,
        CancellationToken(),
    )
    assert page.has_more is True
    assert page.next_cursor == "v1w:1:1:0"


def test_licensed_weibo_reuses_bounded_mobile_card_contract() -> None:
    payload = {
        "ok": 1,
        "data": {
            "cards": [
                {
                    "card_type": 9,
                    "mblog": {
                        "id": "B123456",
                        "text": "hello <br>world",
                        "attitudes_count": 3,
                        "comments_count": 2,
                        "reposts_count": 1,
                        "user": {"id": "author-1"},
                    },
                }
            ],
            "cardlistInfo": {"has_more": False},
        },
    }
    cards, has_more = _result_cards(payload)
    post = _post_from_card(cards[0])
    assert has_more is False
    assert post is not None
    assert post.canonical_url == "https://m.weibo.cn/detail/B123456"
    assert post.metrics == {
        "like_count": 3,
        "comment_count": 2,
        "share_count": 1,
    }
    record = normalize_weibo_post(
        post,
        IdentityPseudonymizer(b"w" * 32),
        provider_id="licensed_weibo",
    )
    assert record.provenance["provider_id"] == "licensed_weibo"
    assert "containerid=100103type%3D1" in _search_url("test", 1, SearchType.DEFAULT)
