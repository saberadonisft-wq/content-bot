import threading

import numpy as np
import pytest

from app.services.gemini_media import (
    ChunkPolicy,
    NoSpeechBoundary,
    SileroStream,
    plan_chunks,
    prepare_manifest,
)


def test_pause_near_target_and_source_mapping():
    manifest = plan_chunks(368_000, [(0, 113_000), (115_000, 242_000), (244_000, 368_000)], ChunkPolicy(), fingerprint="test", source_start_ms=250)
    chunks = manifest["chunks"]
    assert chunks[0]["core_end_ms"] == 114_800
    assert chunks[1]["media_start_ms"] == chunks[0]["core_end_ms"] - 2000
    assert chunks[-1]["core_end_ms"] == 368_000
    assert manifest["source_start_ms"] == 250
    assert sum(c["core_end_ms"] - c["core_start_ms"] for c in chunks) == 368_000


def test_never_force_cut_continuous_speech_at_resource_limit():
    with pytest.raises(NoSpeechBoundary):
        plan_chunks(900_000, [(0, 900_000)], ChunkPolicy(max_chunk_ms=600_000), fingerprint="test")
    result = plan_chunks(350_000, [(0, 350_000)], ChunkPolicy(), fingerprint="test")
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["needs_review"]


def test_extended_pause_and_silent_video():
    result = plan_chunks(400_000, [(0, 181_000), (183_000, 400_000)], ChunkPolicy(), fingerprint="test")
    assert result["chunks"][0]["boundary_reason"] == "extended_speech_pause"
    assert 181_000 < result["chunks"][0]["core_end_ms"] < 183_000
    silent = plan_chunks(400_000, [], ChunkPolicy(), fingerprint="silent")
    assert silent["chunks"][0]["core_end_ms"] == 120_000


def test_streaming_remainder_reset_and_multiple_videos():
    model = SileroStream()
    data = np.random.default_rng(7).normal(0, 0.1, 16000 + 137).astype(np.float32)
    expected = model.feed(data, final=True)
    model.reset()
    observed = []
    for start in range(0, len(data), 113):
        observed.extend(model.feed(data[start:start + 113]))
    observed.extend(model.feed([], final=True))
    assert observed == expected
    model.reset()
    assert model.feed(data, final=True) == expected
    assert model.pending.size == 0


def test_model_checksum_fails_closed(tmp_path):
    path = tmp_path / "model.onnx"
    path.write_bytes(b"invalid model")
    with pytest.raises(ValueError, match="checksum"):
        SileroStream(path)


def test_manifest_reuses_speech_when_policy_changes_and_rejects_bad_cache(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake media")
    monkeypatch.setattr("app.services.gemini_media.extract_audio", lambda *a, **k: None)
    calls = []
    def detect(*args, **kwargs):
        calls.append(1)
        return [(0, 110_000), (112_000, 220_000)]
    monkeypatch.setattr("app.services.gemini_media.detect_speech", detect)
    media = {"duration_ms": 300_000, "fingerprint": "a"}
    cache = tmp_path / "cache"
    first = prepare_manifest(video, media, cache, ChunkPolicy(), cancel_event=threading.Event())
    second = prepare_manifest(video, media, cache, ChunkPolicy(target_ms=100_000), cancel_event=threading.Event())
    assert len(calls) == 1
    assert first["id"] != second["id"]
    next(cache.glob("*/speech.json")).write_text('{"speech":"bad"}')
    third = prepare_manifest(video, media, cache, ChunkPolicy(), cancel_event=threading.Event())
    assert third["id"] == first["id"]
    assert len(calls) == 2
