from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.voiceover import build_voiceover_router
from app.middleware.auth import get_current_user
from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.models import VoiceClip, VoiceDocument, VoiceProfile
from app.services.voiceover.store import VoiceStore, generation_hash, normalized_text


def document():
    return VoiceDocument(
        project_id="a" * 16,
        video_fingerprint="fixture",
        profile=VoiceProfile(id="fixture", name="Fixture", preset="fixture"),
        text_normalization="vi-context-v1",
        clips=[
            VoiceClip(
                id="one",
                spoken_text="Giá 1.000.000.000.000 đồng.",
                source_text="Displayed text",
                start_ms=0,
                end_ms=2000,
            ),
            VoiceClip(id="two", spoken_text="Xin chào", start_ms=2500, end_ms=3500),
        ],
    )


def test_dictionary_has_priority_without_cascading_or_changing_punctuation():
    assert (
        normalized_text(
            "AI, giá 20 đồng.", {"AI": "API", "API": "wrong"}, "vi-context-v1"
        )
        == "API, giá hai mươi đồng."
    )
    assert (
        normalized_text(
            "RTX 3050 giá 20 đồng", {"RTX 3050": "giọng tùy chọn"}, "vi-context-v1"
        )
        == "giọng tùy chọn giá hai mươi đồng"
    )


def test_generation_manifest_and_hash_use_the_same_normalized_text():
    doc = document()
    original = doc.model_dump()
    manifest = VoiceManager.manifest(SimpleNamespace(), "fixture", doc, "cpu")
    assert manifest["clips"][0]["text"] == "Giá một nghìn tỷ đồng."
    assert doc.model_dump() == original
    hashes = [generation_hash(doc, c, "cpu") for c in doc.clips]
    doc.clips[0].spoken_text = "Giá 2.000 đồng"
    assert generation_hash(doc, doc.clips[0], "cpu") != hashes[0]
    assert generation_hash(doc, doc.clips[1], "cpu") == hashes[1]
    doc.text_normalization = "off"
    assert generation_hash(doc, doc.clips[1], "cpu") != hashes[1]


def test_preview_api_and_audio_preview_share_normalization_and_dictionary(tmp_path):
    store = VoiceStore(tmp_path)
    captured = []

    def start(owner, project, device):
        doc = store.get_document(owner, project)
        captured.append(VoiceManager.manifest(SimpleNamespace(), owner, doc, device))
        return {"id": "fixture"}

    app = FastAPI()
    app.include_router(
        build_voiceover_router(SimpleNamespace(store=store, start=start))
    )
    app.dependency_overrides[get_current_user] = lambda: {"sub": "fixture"}
    payload = {
        "text": "AI giá 20 đồng.",
        "pronunciation": {"AI": "tùy chọn"},
        "text_normalization": "vi-context-v1",
    }
    with TestClient(app) as client:
        text = client.post("/api/v1/voiceover/text-preview", json=payload)
        assert text.status_code == 200
        assert text.json()["text"] == "tùy chọn giá hai mươi đồng."
        audio = client.post(
            "/api/v1/voiceover/preview",
            json={**payload, "profile": document().profile.model_dump()},
        )
        assert audio.status_code == 200, audio.text
        assert captured[0]["clips"][0]["text"] == text.json()["text"]
        assert (
            client.post(
                "/api/v1/voiceover/text-preview",
                json={**payload, "pronunciation": {"x" * 101: "bad"}},
            ).status_code
            == 422
        )
