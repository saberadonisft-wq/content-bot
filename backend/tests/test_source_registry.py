from fastapi.testclient import TestClient

from app import main
from app.crawlers import SOURCE_REGISTRY
from app.crawlers.contracts import ImplementationState, Operation
from app.crawlers.registry import CANONICAL_SOURCE_IDS
from app.services.channel_scans import CHANNEL_SCANNERS
from app.services.connectors import default_connectors


def test_registry_has_exact_canonical_order_and_aliases() -> None:
    assert tuple(manifest.id for manifest in SOURCE_REGISTRY) == CANONICAL_SOURCE_IDS
    assert len(CANONICAL_SOURCE_IDS) == 17
    assert SOURCE_REGISTRY.resolve_id("dy") == "douyin"
    assert SOURCE_REGISTRY.resolve_id("ks") == "kuaishou"
    assert SOURCE_REGISTRY.resolve_id("bili") == "bilibili"
    assert SOURCE_REGISTRY.resolve_id("wb") == "weibo"
    assert SOURCE_REGISTRY.resolve_id("xhs") == "xhs"


def test_connector_registry_matches_manifest_ids_without_noop_claims() -> None:
    connectors = default_connectors()
    assert tuple(connectors) == CANONICAL_SOURCE_IDS
    SOURCE_REGISTRY.validate_bindings(
        connectors, CHANNEL_SCANNERS, renderer_ids={"x", "tiktok"}
    )
    assert SOURCE_REGISTRY.executable("x", Operation.RENDER_EMBED)
    assert SOURCE_REGISTRY.executable("x", Operation.SEARCH)
    assert SOURCE_REGISTRY.executable("x", Operation.SCAN_CHANNEL)
    assert not SOURCE_REGISTRY.executable("tiktok", Operation.SEARCH)
    assert SOURCE_REGISTRY.executable("tiktok", Operation.SCAN_CHANNEL)
    assert SOURCE_REGISTRY.executable("tiktok", Operation.FETCH_DETAIL)
    assert SOURCE_REGISTRY.executable("tiktok", Operation.RENDER_EMBED)
    assert not SOURCE_REGISTRY.executable("facebook", Operation.SEARCH)
    assert SOURCE_REGISTRY.executable("facebook", Operation.SCAN_CHANNEL)
    assert SOURCE_REGISTRY.executable("instagram", Operation.SEARCH)
    assert SOURCE_REGISTRY.executable("bilibili", Operation.LIST_COMMENTS)
    assert (
        SOURCE_REGISTRY.executable_provider(
            "bilibili", Operation.LIST_COMMENTS
        )[0]
        == "cbce_bilibili"
    )

    for manifest in SOURCE_REGISTRY:
        for provider in manifest.providers:
            for spec in provider.operations:
                if spec.implementation is ImplementationState.IMPLEMENTED:
                    assert spec.handler_key


def test_every_operation_metric_is_declared_by_its_source_manifest() -> None:
    for manifest in SOURCE_REGISTRY:
        metric_ids = {metric.id for metric in manifest.metrics}
        for provider in manifest.providers:
            for spec in provider.operations:
                assert set(spec.metric_ids).issubset(metric_ids), (
                    manifest.id,
                    provider.id,
                    spec.operation,
                )


def test_bridge_aliases_are_transport_only_not_persisted_source_ids() -> None:
    assert set(CANONICAL_SOURCE_IDS).isdisjoint({"dy", "ks", "bili", "wb"})
    assert SOURCE_REGISTRY.require("dy").id == "douyin"


def test_operation_provider_resolution_does_not_blindly_use_source_default() -> None:
    assert SOURCE_REGISTRY.executable_provider("x", Operation.SEARCH)[0] == "x_api"
    assert (
        SOURCE_REGISTRY.executable_provider("x", Operation.RENDER_EMBED)[0]
        == "x_embed"
    )
    assert (
        SOURCE_REGISTRY.executable_provider("instagram", Operation.SEARCH)[0]
        == "instagram_hashtag"
    )
    assert (
        SOURCE_REGISTRY.executable_provider(
            "tieba",
            Operation.SEARCH,
            preferred_provider_id="cbce_tieba",
        )[0]
        == "cbce_tieba"
    )


def test_source_catalog_is_manifest_driven_and_operation_specific(monkeypatch) -> None:
    monkeypatch.setattr("app.services.connectors.settings.x_bearer_token", None)
    with TestClient(main.app) as client:
        response = client.get("/api/v1/sources")
    assert response.status_code == 200
    rows = response.json()
    assert tuple(row["id"] for row in rows) == CANONICAL_SOURCE_IDS
    assert [row["order"] for row in rows] == list(range(1, 18))
    by_id = {row["id"]: row for row in rows}

    x_operations = {(op["provider_id"], op["id"]): op for op in by_id["x"]["operations"]}
    assert x_operations[("x_embed", "render_embed")]["enabled"] is True
    assert x_operations[("x_api", "search")]["enabled"] is False
    assert x_operations[("x_api", "search")]["implementation"] == "implemented"
    assert x_operations[("x_api", "search")]["reason_code"] == "LOCAL_PREREQUISITE_MISSING"
    assert x_operations[("x_api", "scan_channel")]["enabled"] is False
    assert x_operations[("x_api", "scan_channel")]["implementation"] == "implemented"
    assert by_id["x"]["watchlist_filter"] is True
    assert by_id["x"]["state"] == "ready"
    assert by_id["tiktok"]["global_search"] is False
    tiktok_operations = {
        (operation["provider_id"], operation["id"]): operation
        for operation in by_id["tiktok"]["operations"]
    }
    assert tiktok_operations[("tiktok_embed", "render_embed")]["enabled"] is True
    assert by_id["facebook"]["watchlist_filter"] is True
    licensed_weibo = next(
        operation
        for operation in by_id["weibo"]["operations"]
        if operation["provider_id"] == "licensed_weibo"
        and operation["id"] == "search"
    )
    assert licensed_weibo["budget_limits"] == {
        "max_items": 100,
        "max_requests": 100,
        "deadline_seconds": 900,
    }


def test_keyword_api_resolves_legacy_alias_without_persisting_it() -> None:
    with TestClient(main.app) as client:
        response = client.post(
            "/api/v1/keywords",
            json={
                "name": "Canonical alias topic",
                "source_ids": ["dy", "douyin", "bili"],
                "enabled": False,
            },
        )
        assert response.status_code == 201
        topic = response.json()
        assert topic["source_ids"] == ["douyin", "bilibili"]
        assert client.delete(f"/api/v1/keywords/{topic['id']}").status_code == 204
