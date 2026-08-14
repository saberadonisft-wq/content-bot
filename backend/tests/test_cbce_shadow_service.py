import asyncio
import json

import pytest

from app.config import settings
from app.crawlers.runtime import ShadowAcceptancePolicy
from app.services.cbce_shadow import (
    ShadowProviderTimeout,
    compare_connectors_without_persistence,
)
from app.services.connectors import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from scripts.cbce_shadow_bilibili import (
    RUN_ROOT_ENV,
    _create_shadow_scratch,
    _remove_shadow_scratch,
)


class FakeConnector(SourceConnector):
    source_id = "bilibili"
    label = "Bilibili"
    group = "test"
    capabilities = ConnectorCapabilities(global_search=True)

    def __init__(self, ids):
        self.ids = ids
        self.queries = []

    async def healthcheck(self):
        return ConnectorStatus("ready", "test")

    async def search(self, query, checkpoint=None):
        self.queries.append((query, checkpoint))
        for external_id in self.ids:
            yield RawContentItem(
                external_id=external_id,
                canonical_url=f"https://www.bilibili.com/video/{external_id}",
                title=f"title-{external_id}",
                metrics={"view_count": 1, "unapproved_metric": 99},
            )


class HangingConnector(FakeConnector):
    def __init__(self):
        super().__init__([])
        self.cleaned = asyncio.Event()

    async def search(self, query, checkpoint=None):
        try:
            await asyncio.Event().wait()
            if False:
                yield RawContentItem("", "", "")
        finally:
            self.cleaned.set()


def test_shadow_service_does_not_reuse_checkpoint_or_expose_items() -> None:
    baseline = FakeConnector(["BV1", "BV2"])
    candidate = FakeConnector(["BV1", "BV3"])
    original = SearchQuery(
        keyword_id=7,
        name="game",
        include_terms=["games"],
        max_items=10,
        checkpoint_tracker=object(),
        legacy_checkpoint={"cursor": "secret-provider-cursor"},
    )
    result = asyncio.run(
        compare_connectors_without_persistence(
            baseline,
            candidate,
            original,
            secret_key=b"a" * 32,
            allowed_metric_keys=("view_count",),
            policy=ShadowAcceptancePolicy(
                min_baseline_id_recall=0.5,
                max_count_delta_ratio=0.5,
            ),
        )
    )

    assert result.accepted is True
    for connector in (baseline, candidate):
        shadow_query, checkpoint = connector.queries[0]
        assert shadow_query is not original
        assert shadow_query.checkpoint_tracker is None
        assert shadow_query.legacy_checkpoint == {}
        assert checkpoint is None
    output = json.dumps(result.as_dict(), sort_keys=True)
    assert "BV1" not in output
    assert "secret-provider-cursor" not in output
    assert "unapproved_metric" not in output


def test_shadow_service_rejects_mixed_sources() -> None:
    baseline = FakeConnector([])
    candidate = FakeConnector([])
    candidate.source_id = "youtube"

    try:
        asyncio.run(
            compare_connectors_without_persistence(
                baseline,
                candidate,
                SearchQuery(0, "game", [], 1),
                secret_key=b"a" * 32,
                allowed_metric_keys=(),
            )
        )
    except ValueError as exc:
        assert "same source" in str(exc)
    else:
        raise AssertionError("mixed source shadow providers were accepted")


def test_shadow_timeout_unwinds_provider_before_returning() -> None:
    baseline = HangingConnector()

    async def run():
        try:
            await compare_connectors_without_persistence(
                baseline,
                FakeConnector(["BV1"]),
                SearchQuery(0, "game", [], 1),
                secret_key=b"a" * 32,
                allowed_metric_keys=(),
                provider_timeout_seconds=0.01,
            )
        except ShadowProviderTimeout as exc:
            assert exc.reason_code == "BASELINE_TIMEOUT"
        else:
            raise AssertionError("hanging provider did not time out")
        assert baseline.cleaned.is_set()

    asyncio.run(run())


def test_shadow_scratch_is_owned_bounded_and_removed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    root = _create_shadow_scratch()
    artifact = root / "provider" / "raw.jsonl"
    artifact.parent.mkdir()
    artifact.write_text("temporary", encoding="utf-8")

    _remove_shadow_scratch(root)

    assert not root.exists()
    assert RUN_ROOT_ENV == "CONTENT_BOT_MEDIACRAWLER_RUN_ROOT"


def test_shadow_scratch_cleanup_rejects_unowned_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    outside = tmp_path / ("a" * 32)
    outside.mkdir()

    with pytest.raises(ValueError, match="SHADOW_SCRATCH_PATH_INVALID"):
        _remove_shadow_scratch(outside)

    assert outside.exists()
