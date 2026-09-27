"""Translation jobs retain partial versions and stay bound to their video."""

import time

import pytest
from fastapi.testclient import TestClient

from app.api import subtitles as api
from app.main import app
from app.services.subtitle_translate import SubtitleTranslateError
from app.services.subtitle_versions import SubtitleVersionStore


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/subtitles/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["state"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("Translation job did not finish")


@pytest.mark.parametrize("version_disk_full", [False, True])
def test_failed_translation_exposes_partial_version_or_explicit_save_error(
    tmp_path, monkeypatch, version_disk_full
):
    store = SubtitleVersionStore(tmp_path / "versions")
    monkeypatch.setattr(api, "SubtitleVersionStore", lambda root: store)
    doc = {
        "document_role": "source",
        "language": "en",
        "revision": 7,
        "run_id": "source-run",
        "segments": [
            {"id": "c1", "start_ms": 0, "end_ms": 1000, "text": "Hello"},
            {"id": "c2", "start_ms": 1500, "end_ms": 2200, "text": "Goodbye"},
        ],
    }
    video_id = "a" * 16
    original = store.save(video_id, doc, source="manual")

    def fail_translation(service, document, **kwargs):
        partial = document.model_dump()
        partial.update(
            document_role="translation",
            language="vi",
            source_revision=7,
            source_run_id="source-run",
        )
        partial["segments"] = [
            {**partial["segments"][0], "text": "Xin chào", "source_text": "Hello"}
        ]
        kwargs["context"].update_details(
            {"translated_count": 1, "total_cues": 2, "resume_available": True}
        )
        raise SubtitleTranslateError(
            "fixture batch 2 failed",
            partial_result={
                "document": partial,
                "translated_count": 1,
                "total_count": 2,
                "checkpointed_count": 1,
            },
        )

    monkeypatch.setattr(api, "translate_source_document", fail_translation)
    if version_disk_full:

        def unavailable(*args, **kwargs):
            raise OSError("fixture version disk full")

        monkeypatch.setattr(store, "save", unavailable)
    with TestClient(app) as client:
        submitted = client.post(
            "/api/v1/subtitles/v2/translate/gemini",
            json={"video_id": video_id, "document": doc},
        )
        assert submitted.status_code == 200
        job = wait_job(client, submitted.json()["id"])
        assert job["state"] == "failed"
        assert "fixture batch 2 failed" in job["error"]
        if version_disk_full:
            assert job["details"]["partial_version_save_failed"] is True
            assert "Không lưu được phiên bản" in job["error"]
            assert "partial_version_id" not in job["details"]
        else:
            version = client.get(
                f"/api/v1/subtitles/videos/{video_id}/versions/{job['details']['partial_version_id']}"
            )
            assert version.status_code == 200
            partial_doc = version.json()["document"]
            assert partial_doc["source_revision"] == 7
            assert [(cue["id"], cue["text"]) for cue in partial_doc["segments"]] == [
                ("c1", "Xin chào")
            ]
        assert (
            store.load(video_id, original["id"])["document"]["segments"][0]["text"]
            == "Hello"
        )


def test_identical_translation_for_two_videos_saves_independent_versions(
    tmp_path, monkeypatch
):
    store = SubtitleVersionStore(tmp_path / "versions")
    monkeypatch.setattr(api, "SubtitleVersionStore", lambda root: store)
    calls = []

    def translate(service, document, **kwargs):
        calls.append(kwargs["video_id"])
        return {"document": document.model_dump(), "translated_count": 1}

    monkeypatch.setattr(api, "translate_source_document", translate)
    doc = {
        "segments": [{"id": "c1", "start_ms": 0, "end_ms": 1000, "text": "Xin chào"}]
    }
    with TestClient(app) as client:
        jobs = []
        for video_id in ["a" * 16, "b" * 16]:
            submitted = client.post(
                "/api/v1/subtitles/v2/translate/gemini",
                json={"video_id": video_id, "document": doc},
            )
            assert submitted.status_code == 200
            job = wait_job(client, submitted.json()["id"])
            assert job["state"] == "succeeded"
            assert (
                store.load(video_id, job["result"]["version_id"])["video_id"]
                == video_id
            )
            jobs.append(job["id"])
        assert jobs[0] != jobs[1]
        assert calls == ["a" * 16, "b" * 16]
