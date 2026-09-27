from contextlib import contextmanager

import pytest

from app.services import subtitle_ocr_supervisor as supervisor
from app.services.subtitle_jobs import SubtitleJobCanceled
from app.services.subtitle_ocr import SubtitleOcrError


def _run_options(tmp_path, **overrides):
    values = {
        "video_path": tmp_path / "input.mp4",
        "region": {"x": 10, "y": 70, "width": 80, "height": 20},
        "media": {"fingerprint": "test", "duration_ms": 1000},
        "device_policy": "auto",
        "gpu_site_packages": tmp_path / "gpu-profile",
        "worker_timeout_seconds": 60,
        "context": None,
        "cache_dir": tmp_path / "cache",
        "source_language": "zh",
        "sample_fps": 3.0,
        "min_duration_ms": 300,
        "max_gap_ms": 250,
        "auto_probe": False,
    }
    values.update(overrides)
    return values


@contextmanager
def _unlocked_slot(*_args, **_kwargs):
    yield


def test_auto_uses_cpu_with_explicit_warning_when_gpu_profile_is_missing(
    tmp_path, monkeypatch
):
    cpu_calls = []

    def run_cpu(*args, **kwargs):
        cpu_calls.append(kwargs)
        return {"warnings": []}

    monkeypatch.setattr(supervisor, "_run_cpu", run_cpu)
    result = supervisor.run_subtitle_ocr_job(**_run_options(tmp_path))

    assert result["warnings"] == []
    assert cpu_calls[0]["fallback_reason"] == "ocr_runtime_unavailable"
    assert cpu_calls[0]["requested_device"] == "auto"


def test_cuda_policy_fails_when_gpu_profile_is_missing(tmp_path):
    with pytest.raises(SubtitleOcrError, match="Runtime OCR GPU chưa được cài"):
        supervisor.run_subtitle_ocr_job(
            **_run_options(tmp_path, device_policy="cuda")
        )


def test_auto_restarts_on_cpu_after_gpu_provider_failure(tmp_path, monkeypatch):
    profile = tmp_path / "gpu-profile"
    profile.mkdir()
    cpu_calls = []

    def fail_gpu(*_args, **_kwargs):
        raise SubtitleOcrError("provider mismatch", code="ocr_provider_mismatch")

    def run_cpu(*_args, **kwargs):
        cpu_calls.append(kwargs)
        return {"warnings": []}

    monkeypatch.setattr(supervisor, "_run_gpu_worker", fail_gpu)
    monkeypatch.setattr(supervisor, "_run_cpu", run_cpu)
    monkeypatch.setattr(supervisor, "gpu_model_slot", _unlocked_slot)
    result = supervisor.run_subtitle_ocr_job(
        **_run_options(tmp_path, gpu_site_packages=profile)
    )

    assert cpu_calls[0]["fallback_reason"] == "ocr_provider_mismatch"
    assert result["warnings"] == []


def test_cpu_fallback_result_records_effective_runtime_and_warning(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        supervisor,
        "extract_subtitles_ocr",
        lambda *_args, **_kwargs: {"warnings": [], "document": {}},
    )
    options = _run_options(tmp_path)
    result = supervisor._run_cpu(
        options["video_path"],
        options["region"],
        options["media"],
        context=None,
        cache_dir=options["cache_dir"],
        source_language=options["source_language"],
        sample_fps=options["sample_fps"],
        min_duration_ms=options["min_duration_ms"],
        max_gap_ms=options["max_gap_ms"],
        auto_probe=options["auto_probe"],
        requested_device="auto",
        fallback_reason="ocr_provider_mismatch",
    )

    assert result["runtime"]["effective_device"] == "cpu"
    assert result["runtime"]["fallback_reason"] == "ocr_provider_mismatch"
    assert result["warnings"][0]["code"] == "ocr_cpu_fallback"


def test_gpu_cancellation_never_retries_on_cpu(tmp_path, monkeypatch):
    profile = tmp_path / "gpu-profile"
    profile.mkdir()

    def cancel_gpu(*_args, **_kwargs):
        raise SubtitleJobCanceled("Đã hủy OCR.")

    monkeypatch.setattr(supervisor, "_run_gpu_worker", cancel_gpu)
    monkeypatch.setattr(supervisor, "gpu_model_slot", _unlocked_slot)
    monkeypatch.setattr(
        supervisor,
        "_run_cpu",
        lambda *_args, **_kwargs: pytest.fail("CPU fallback must not run after cancel"),
    )

    with pytest.raises(SubtitleJobCanceled, match="Đã hủy OCR"):
        supervisor.run_subtitle_ocr_job(
            **_run_options(tmp_path, gpu_site_packages=profile)
        )


def test_gpu_slot_covers_entire_worker_lifetime(tmp_path, monkeypatch):
    profile = tmp_path / "gpu-profile"
    profile.mkdir()
    lock_held = False

    @contextmanager
    def slot(*_args, **_kwargs):
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def run_worker(*_args, **_kwargs):
        assert lock_held
        return {"runtime": {"effective_device": "cuda"}}

    monkeypatch.setattr(supervisor, "gpu_model_slot", slot)
    monkeypatch.setattr(supervisor, "_run_gpu_worker", run_worker)
    result = supervisor.run_subtitle_ocr_job(
        **_run_options(tmp_path, gpu_site_packages=profile)
    )

    assert not lock_held
    assert result["runtime"]["effective_device"] == "cuda"
    assert result["runtime"]["gpu_slot_wait_seconds"] >= 0


def test_auto_retries_on_cpu_when_gpu_slot_times_out(tmp_path, monkeypatch):
    profile = tmp_path / "gpu-profile"
    profile.mkdir()
    cpu_calls = []

    @contextmanager
    def timeout_slot(*_args, **_kwargs):
        raise TimeoutError("GPU slot is busy")
        yield

    def run_cpu(*_args, **kwargs):
        cpu_calls.append(kwargs)
        return {"warnings": []}

    monkeypatch.setattr(supervisor, "gpu_model_slot", timeout_slot)
    monkeypatch.setattr(supervisor, "_run_cpu", run_cpu)
    result = supervisor.run_subtitle_ocr_job(
        **_run_options(tmp_path, gpu_site_packages=profile)
    )

    assert cpu_calls[0]["fallback_reason"] == "ocr_gpu_wait_timeout"
    assert result["warnings"] == []


def test_runtime_signature_changes_when_gpu_profile_changes(tmp_path):
    profile = tmp_path / "gpu-profile"
    missing = supervisor.subtitle_ocr_runtime_signature("auto", profile)
    profile.mkdir()
    manifest = profile / "runtime-manifest.json"
    manifest.write_text('{"onnxruntime_gpu":"1.26.0"}', encoding="utf-8")
    installed = supervisor.subtitle_ocr_runtime_signature("auto", profile)
    manifest.write_text('{"onnxruntime_gpu":"1.27.0"}', encoding="utf-8")
    upgraded = supervisor.subtitle_ocr_runtime_signature("auto", profile)

    assert missing != installed
    assert installed != upgraded
