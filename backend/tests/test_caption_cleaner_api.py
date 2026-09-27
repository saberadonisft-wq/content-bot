from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.items import router
from app.application_services import get_services
from app.sqlite_store import SQLiteStore


def test_caption_clean_endpoint_preserves_original_and_returns_editable_result():
    app = FastAPI()
    app.include_router(router)
    text = "Phim mới #review [话题]# VX: demo_account 13800138000"
    with TestClient(app) as client:
        response = client.post("/api/v1/captions/clean", json={"text": text})

    assert response.status_code == 200
    payload = response.json()
    assert payload["original_text"] == text
    assert "demo_account" not in payload["cleaned_text"]
    assert "13800138000" not in payload["cleaned_text"]
    assert payload["hashtags"] == ["review"]
    assert payload["safe_filename"]
    assert payload["changed"] is True


def test_caption_clean_endpoint_bounds_input():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/v1/captions/clean", json={"text": "x" * 20_001})
    assert response.status_code == 422


def test_caption_apply_persists_edit_without_overwriting_ingested_source(tmp_path):
    store = SQLiteStore(tmp_path / "items.db")
    store.initialize()
    item = store.save_item(
        {
            "source_id": "web",
            "external_id": "caption-1",
            "canonical_url": "https://example.test/caption-1",
            "title": "Tiêu đề gốc",
            "body_snippet": "Nội dung gốc",
        }
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_services] = lambda: SimpleNamespace(store=store)
    try:
        with TestClient(app) as client:
            first = client.post(
                f"/api/v1/items/{item['id']}/caption",
                json={"edited_text": "Tiêu đề đã sạch\nNội dung đã sửa"},
            )
            assert first.status_code == 200
            payload = first.json()
            assert payload["caption_original"] == "Tiêu đề gốc\nNội dung gốc"
            assert payload["caption_edited"] == "Tiêu đề đã sạch\nNội dung đã sửa"

            second = client.post(
                f"/api/v1/items/{item['id']}/caption",
                json={"edited_text": "Bản chỉnh sửa lần hai"},
            )
            assert second.status_code == 200
            assert second.json()["caption_original"] == payload["caption_original"]
            assert client.post("/api/v1/items/999999/caption", json={"edited_text": "x"}).status_code == 404
    finally:
        app.dependency_overrides.clear()

    saved = store.item(item["id"])
    assert saved["title"] == "Tiêu đề gốc"
    assert saved["body_snippet"] == "Nội dung gốc"
    assert saved["caption_edited"] == "Bản chỉnh sửa lần hai"
