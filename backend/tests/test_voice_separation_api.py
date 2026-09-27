import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.voiceover import build_voiceover_router
from app.middleware.auth import get_current_user
from app.services.subtitle_jobs import SubtitleJobManager
from app.services.voiceover.models import VoiceDocument, VoiceProfile
from app.services.voiceover.separation_store import SeparationStore
from app.services.voiceover.store import VoiceStore


def wav(path: Path, frequency=440):
    t = np.arange(16000, dtype=np.float64) / 16000
    samples = (.2 * np.sin(2 * np.pi * frequency * t)).astype("<f4")
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes((np.column_stack([samples, samples]) * 32767).astype("<i2").tobytes())
    return path


def test_selected_stem_is_owner_scoped_checksum_verified_and_clearable(tmp_path):
    store = VoiceStore(tmp_path / "voice")
    project = "a" * 20
    document = VoiceDocument(
        project_id=project,
        video_fingerprint="video-fingerprint",
        profile=VoiceProfile(id="fixture", name="Fixture", preset="fixture"),
    )
    store.save_document("owner", document)
    source = wav(tmp_path / "background.wav")
    vocals = wav(tmp_path / "vocals.wav", 700)
    separations = SeparationStore(store.root / "separation-cache")
    manifest = separations.publish(
        "owner",
        source_fingerprint=document.video_fingerprint,
        source_audio_checksum="b" * 64,
        cache_key="c" * 64,
        result={"method": "center_reduction", "warnings": ["listen"],
                "stems": {"background": str(source), "vocals": str(vocals)}},
    )

    app = FastAPI()
    jobs = SubtitleJobManager(tmp_path / "jobs")
    app.include_router(build_voiceover_router(
        SimpleNamespace(store=store), separation_jobs_provider=lambda: jobs,
    ))
    app.dependency_overrides[get_current_user] = lambda: {"sub": "owner"}
    try:
        with TestClient(app) as client:
            selected = client.post(
                f"/api/v1/voiceover/projects/{project}/background-stems/{manifest['id']}/select",
                json={"gain": 0.7},
            )
            assert selected.status_code == 200, selected.text
            current = store.get_document("owner", project)
            assert current.mix.background_stem_id == manifest["id"]
            assert current.mix.background_gain == 0.7
            fetched = client.get(
                f"/api/v1/voiceover/projects/{project}/separations/stems/{manifest['id']}/background"
            )
            assert fetched.status_code == 200
            assert fetched.content == source.read_bytes()
            listed = client.get(f"/api/v1/voiceover/projects/{project}/separations")
            assert listed.status_code == 200
            assert listed.json()[0]["stem_id"] == manifest["id"]
            cleared = client.delete(
                f"/api/v1/voiceover/projects/{project}/background-stems/selection"
            )
            assert cleared.status_code == 200
            assert store.get_document("owner", project).mix.background_stem_id is None
    finally:
        jobs.shutdown(wait=True)


def test_selection_rejects_other_video_and_tampered_stem(tmp_path):
    store = VoiceStore(tmp_path / "voice")
    project = "b" * 20
    document = VoiceDocument(
        project_id=project,
        video_fingerprint="video-a",
        profile=VoiceProfile(id="fixture", name="Fixture", preset="fixture"),
    )
    store.save_document("owner", document)
    source = wav(tmp_path / "background.wav")
    separations = SeparationStore(store.root / "separation-cache")
    manifest = separations.publish(
        "owner", source_fingerprint="video-b", source_audio_checksum="d" * 64,
        cache_key="e" * 64, result={"method": "center_reduction", "stems": {"background": str(source)}},
    )
    path = separations.file("owner", manifest["id"])
    path.write_bytes(b"tampered")
    app = FastAPI()
    app.include_router(build_voiceover_router(SimpleNamespace(store=store)))
    app.dependency_overrides[get_current_user] = lambda: {"sub": "owner"}
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/voiceover/projects/{project}/background-stems/{manifest['id']}/select",
            json={"gain": 1},
        )
    assert response.status_code == 409
    assert store.get_document("owner", project).mix.background_stem_id is None


def test_separation_job_dedupe_is_scoped_to_owner_and_project(tmp_path):
    store = VoiceStore(tmp_path / "voice")
    project = "c" * 20
    for owner in ("owner-one", "owner-two"):
        store.save_document(owner, VoiceDocument(
            project_id=project,
            video_fingerprint="same-video",
            profile=VoiceProfile(id="fixture", name="Fixture", preset="fixture"),
        ))

    class FakeJobs:
        def __init__(self):
            self.calls = []

        def submit(self, kind, key, runner, details=None):
            self.calls.append((kind, key, details))
            return {"id": f"job-{len(self.calls)}", "kind": kind, "details": details}

    jobs = FakeJobs()
    app = FastAPI()
    app.include_router(build_voiceover_router(
        SimpleNamespace(store=store), separation_jobs_provider=lambda: jobs,
    ))
    current_owner = {"value": "owner-one"}
    app.dependency_overrides[get_current_user] = lambda: {"sub": current_owner["value"]}
    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/voiceover/projects/{project}/separations",
            json={"method": "center_reduction", "device": "cpu"},
        )
        current_owner["value"] = "owner-two"
        second = client.post(
            f"/api/v1/voiceover/projects/{project}/separations",
            json={"method": "center_reduction", "device": "cpu"},
        )
    assert first.status_code == 200 and second.status_code == 200
    assert jobs.calls[0][1] != jobs.calls[1][1]
    assert jobs.calls[0][2] == {"project_id": project, "owner": "owner-one"}
    assert jobs.calls[1][2] == {"project_id": project, "owner": "owner-two"}
