from fastapi.testclient import TestClient

from app.main import app


def test_health_and_keyword_lifecycle() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").json()["status"] == "ready"
        created = client.post(
            "/api/v1/keywords",
            json={
                "name": "Test Game Signal",
                "include_terms": ["Test Game"],
                "exclude_terms": [],
                "source_ids": ["youtube"],
                "enabled": False,
                "interval_minutes": 360,
                "max_items_per_source": 10,
            },
        )
        assert created.status_code == 201
        keyword_id = created.json()["id"]
        assert client.get("/api/v1/items", params={"keyword_id": keyword_id}).json()["items"] == []
        summary = client.get("/api/v1/insights/summary", params={"keyword_id": keyword_id})
        assert summary.status_code == 200
        assert summary.json()["total_items"] == 0
        assert summary.json()["top_items"] == []
        clusters = client.get("/api/v1/insights/clusters", params={"keyword_id": keyword_id})
        assert clusters.status_code == 200
        assert clusters.json()["total_items"] == 0
        assert clusters.json()["clusters"] == []
        assert client.get("/api/v1/runs", params={"keyword_id": keyword_id}).json() == []
        assert client.get("/api/v1/export.json", params={"keyword_id": keyword_id}).json() == []
        assert client.delete(f"/api/v1/keywords/{keyword_id}").status_code == 204


def test_insight_summary_rejects_unknown_sentiment() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/insights/summary",
            params={"keyword_id": 1, "sentiment": "excited"},
        )
    assert response.status_code == 422


def test_insight_clusters_validates_parameters_and_keyword() -> None:
    with TestClient(app) as client:
        invalid_sentiment = client.get(
            "/api/v1/insights/clusters",
            params={"keyword_id": 1, "sentiment": "excited"},
        )
        invalid_minimum = client.get(
            "/api/v1/insights/clusters",
            params={"keyword_id": 1, "min_items": 1},
        )
        missing_keyword = client.get(
            "/api/v1/insights/clusters",
            params={"keyword_id": 999999},
        )

    assert invalid_sentiment.status_code == 422
    assert invalid_minimum.status_code == 422
    assert missing_keyword.status_code == 404


def test_source_registry_discloses_setup_state() -> None:
    with TestClient(app) as client:
        sources = {source["id"]: source for source in client.get("/api/v1/sources").json()}
    assert {"youtube", "web", "steam", "bluesky", "xhs", "tiktok"}.issubset(sources)
    assert sources["bluesky"]["state"] == "ready"
    assert sources["xhs"]["state"] in {"ready", "setup_required", "not_configured"}
    assert sources["tiktok"]["primary_operation"] == "scan_channel"
    assert sources["x"]["primary_operation"] == "render_embed"


def test_empty_source_selection_defaults_to_public_no_login_sources() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/keywords",
            json={
                "name": "Safe Default Sources",
                "source_ids": [],
                "enabled": False,
                "interval_minutes": 360,
                "max_items_per_source": 10,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert {"web", "steam", "bluesky"}.issubset(payload["source_ids"])
        assert {"xhs", "douyin", "tiktok", "facebook", "instagram"}.isdisjoint(payload["source_ids"])
        assert client.delete(f"/api/v1/keywords/{payload['id']}").status_code == 204


def test_keyword_update_rejects_normalized_duplicate_name() -> None:
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/keywords",
            json={"name": "Duplicate Game", "enabled": False},
        )
        second = client.post(
            "/api/v1/keywords",
            json={"name": "Other Game", "enabled": False},
        )
        assert first.status_code == 201
        assert second.status_code == 201
        duplicate = client.patch(
            f"/api/v1/keywords/{second.json()['id']}",
            json={"name": "duplicate game", "enabled": False},
        )
        assert duplicate.status_code == 409
        assert client.delete(f"/api/v1/keywords/{first.json()['id']}").status_code == 204
        assert client.delete(f"/api/v1/keywords/{second.json()['id']}").status_code == 204
