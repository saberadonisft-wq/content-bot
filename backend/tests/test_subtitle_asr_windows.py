"""Word ownership, bounded audio and cancellation regressions, without downloading models."""

from types import SimpleNamespace as Obj
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services import subtitle_asr as asr
from app.services.subtitle_jobs import SubtitleJobCanceled


def setup_engine(tmp_path, monkeypatch, transcribe):
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    model = Obj(transcribe=transcribe)
    monkeypatch.setattr(asr, "_resolve_whisper_model", lambda *a, **kw: "fixture-model")
    monkeypatch.setattr(asr, "_load_cached_whisper_model", lambda *a: model)
    windows = []

    def read(path, context=None, **kw):
        windows.append(kw)
        return np.zeros(round(kw["duration_ms"] / 1000 * 16000), np.float32)

    monkeypatch.setattr(asr, "_extract_audio_pcm", read)
    return video, windows


def test_auto_language_is_none_and_windows_have_bounded_overlap(tmp_path, monkeypatch):
    calls = []

    def transcribe(audio, **kw):
        calls.append((len(audio), kw))
        return [], Obj(language="en", language_probability=0.9)

    video, windows = setup_engine(tmp_path, monkeypatch, transcribe)
    result = asr.extract_subtitles_asr(
        video,
        {"duration_ms": 145000, "has_audio": True},
        source_language="auto",
        device="cpu",
    )
    assert windows == [
        {"start_ms": 0, "duration_ms": 62000},
        {"start_ms": 58000, "duration_ms": 64000},
        {"start_ms": 118000, "duration_ms": 27000},
    ]
    assert all(
        kw["language"] is None and kw["task"] == "transcribe" and kw["vad_filter"]
        for _, kw in calls
    )
    assert max(n for n, _ in calls) == 64000 * 16
    assert result["metrics"]["windows"] == 3


def test_overlap_word_is_owned_once_and_clamped_with_evidence(tmp_path, monkeypatch):
    info = Obj(language="en", language_probability=0.95)
    before = Obj(word=" Hello", start=59.0, end=59.6, probability=0.95)
    crossing = Obj(word=" world", start=59.9, end=60.2, probability=0.9)
    end = Obj(word=" end", start=4.8, end=5.8, probability=0.85)
    results = iter(
        [
            (
                [
                    Obj(
                        text="Hello world",
                        start=59.0,
                        end=60.2,
                        words=[before, crossing],
                    )
                ],
                info,
            ),
            (
                [
                    Obj(
                        text="world end",
                        start=1.9,
                        end=5.8,
                        words=[
                            Obj(word=" world", start=1.9, end=2.2, probability=0.9),
                            end,
                        ],
                    )
                ],
                info,
            ),
        ]
    )
    video, _ = setup_engine(tmp_path, monkeypatch, lambda *a, **kw: next(results))
    cues = asr.extract_subtitles_asr(
        video, {"duration_ms": 63000, "fingerprint": "fixture"}, device="cpu"
    )["document"]["segments"]
    assert [w["text"] for c in cues for w in c["words"]] == ["Hello", "world", "end"]
    assert [(c["start_ms"], c["end_ms"]) for c in cues] == [
        (59000, 59600),
        (59900, 63000),
    ]
    assert cues[1]["text"] == "world end"
    for cue in cues:
        assert cue["speech_evidence"]["end_ms"] == cue["end_ms"]
        assert all(
            cue["start_ms"] <= w["start_ms"] < w["end_ms"] <= cue["end_ms"]
            for w in cue["words"]
        )


def test_cancellation_from_segment_iterator_remains_canceled(tmp_path, monkeypatch):
    def segments():
        raise SubtitleJobCanceled("canceled in inference")
        yield

    video, _ = setup_engine(
        tmp_path,
        monkeypatch,
        lambda *a, **kw: (segments(), Obj(language="en", language_probability=0.9)),
    )
    with pytest.raises(SubtitleJobCanceled, match="in inference"):
        asr.extract_subtitles_asr(video, {"duration_ms": 1000}, device="cpu")


def test_invalid_language_is_rejected_before_audio_or_model(tmp_path, monkeypatch):
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    load = MagicMock()
    monkeypatch.setattr(asr, "_resolve_whisper_model", load)
    with pytest.raises(asr.SubtitleAsrError, match="không được hỗ trợ"):
        asr.extract_subtitles_asr(
            video, {"duration_ms": 1000}, source_language="made-up"
        )
    load.assert_not_called()


def test_auto_gpu_failure_falls_back_once_and_reports_actual_device(
    tmp_path, monkeypatch
):
    from contextlib import nullcontext

    import ctranslate2

    video, _ = setup_engine(
        tmp_path,
        monkeypatch,
        lambda *a, **kw: ([], Obj(language="en", language_probability=0.9)),
    )
    calls = []

    def load(path, device, compute, threads):
        calls.append(device)
        if device == "cuda":
            raise RuntimeError("CUDA out of memory")
        return Obj(
            transcribe=lambda *a, **kw: (
                [],
                Obj(language="en", language_probability=0.9),
            )
        )

    monkeypatch.setattr(asr, "_load_cached_whisper_model", load)
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(
        ctranslate2,
        "get_supported_compute_types",
        lambda device: {"float16", "int8", "float32"},
    )
    monkeypatch.setattr(asr, "gpu_model_slot", lambda *a: nullcontext())
    result = asr.extract_subtitles_asr(video, {"duration_ms": 1000}, device="auto")
    assert calls == ["cuda", "cpu"]
    assert result["device"] == "cpu"
    assert result["warnings"][0]["code"] == "asr_cpu_fallback"
