"""Signal-level separation checks with real FFmpeg; no AI quality claim from test doubles."""
import json
import sys
import time
import wave
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from app.services import vocal_separator as separator
from app.services import vocal_separator_supervisor as supervisor
from app.services.owned_process import OwnedProcess
from app.services.subtitle_jobs import SubtitleJobCanceled
from app.services.vocal_audio import VocalSeparatorError, separate_windows, wave_stats


def write_wave(path, samples, rate=44100):
    samples = np.asarray(samples)
    if samples.ndim == 1:
        samples = samples[:, None]
    with wave.open(str(path), "wb") as audio:
        audio.setparams((samples.shape[1], 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


def stereo_fixture(path):
    t = np.arange(44100) / 44100
    center = .2 * np.sin(2 * np.pi * 300 * t)
    left = center + .25 * np.sin(2 * np.pi * 900 * t)
    right = center + .25 * np.sin(2 * np.pi * 1600 * t)
    return write_wave(path, np.column_stack([left, right]))


def read_wave(path):
    with wave.open(str(path)) as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), "<i2").reshape(-1, audio.getnchannels()) / 32768


def test_center_reduction_survives_mono_and_exposes_loss_of_centered_sfx(tmp_path):
    source = stereo_fixture(tmp_path / "source.wav")
    output = tmp_path / "output.wav"
    separator.separate_vocals_phase_cancellation(source, output)
    before, after = read_wave(source), read_wave(output)
    expected = (before[:, 0] - before[:, 1]) / 2
    np.testing.assert_allclose(after[:, 0], expected, atol=2 / 32768)
    np.testing.assert_array_equal(after[:, 0], after[:, 1])
    assert wave_stats(output)["mono_rms"] > .1
    # A centered 300 Hz SFX is lost along with centered speech; do not claim it is preserved.
    spectrum = np.abs(np.fft.rfft(after.mean(axis=1)))
    assert spectrum[300] < spectrum[900] * .001
    assert "hiệu ứng" in separator.CENTER_WARNING


@pytest.mark.parametrize("channels", [1, 2])
def test_mono_and_dual_mono_fail_without_replacing_previous_result(tmp_path, channels):
    tone = .5 * np.sin(2 * np.pi * 440 * np.arange(44100) / 44100)
    source = write_wave(tmp_path / "source.wav", np.tile(tone[:, None], (1, channels)))
    output = tmp_path / "output.wav"
    output.write_bytes(b"previous valid asset")
    with pytest.raises(VocalSeparatorError, match="mono"):
        separator.separate_vocals_phase_cancellation(source, output)
    assert output.read_bytes() == b"previous valid asset"
    assert not list(tmp_path.glob(".center-*"))


def test_antiphase_high_level_remains_audible_without_clipping(tmp_path):
    tone = .99 * np.sin(2 * np.pi * 440 * np.arange(44100) / 44100)
    source = write_wave(tmp_path / "source.wav", np.column_stack([tone, -tone]))
    result = separator.separate_vocals(source, tmp_path / "out", prefer_demucs=False)
    assert result["method"] == "center_reduction"
    assert set(result["stems"]) == {"background"}
    assert result["warnings"]
    stats = wave_stats(result["stems"]["background"])
    assert .69 < stats["mono_rms"] < .71
    assert stats["peak"] < 1


def test_corrupt_audio_and_source_alias_do_not_overwrite_files(tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"invalid")
    with pytest.raises(VocalSeparatorError, match="ghi đè"):
        separator.separate_vocals_phase_cancellation(source, source)
    output = tmp_path / "output.wav"
    output.write_bytes(b"previous")
    with pytest.raises(VocalSeparatorError, match="FFmpeg"):
        separator.separate_vocals_phase_cancellation(source, output)
    assert source.read_bytes() == b"invalid"
    assert output.read_bytes() == b"previous"


def test_cancel_between_decode_and_effect_preserves_previous_output(tmp_path):
    class Context:
        def raise_if_canceled(self):
            pass

        def update(self, progress, phase, message):
            if phase == "separation":
                raise SubtitleJobCanceled("cancel effect")

    source = stereo_fixture(tmp_path / "source.wav")
    output = tmp_path / "output.wav"
    output.write_bytes(b"previous")
    with pytest.raises(SubtitleJobCanceled):
        separator.separate_vocals_phase_cancellation(source, output, context=Context())
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".center-*"))


@pytest.mark.parametrize("available", [False, True])
def test_demucs_error_never_silently_uses_center_reduction(tmp_path, monkeypatch, available):
    source = stereo_fixture(tmp_path / "source.wav")
    monkeypatch.setattr(separator, "is_demucs_available", lambda: available)
    with pytest.raises(VocalSeparatorError, match="Runtime|model Demucs"):
        separator.separate_vocals(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_window_ownership_retains_every_sample_and_tail_with_bounded_input(tmp_path):
    rate = 100
    original = np.arange(9501 * 2, dtype=np.float32).reshape(-1, 2) / 20000
    source = tmp_path / "source.f32"
    original.astype("<f4").tofile(source)
    outputs = {name: tmp_path / (name + ".f32") for name in ("vocals", "background")}
    calls = []

    def predictor(chunk):
        calls.append(chunk.shape[-1])
        return {"vocals": chunk * .25, "background": chunk * .75}

    frames, gain = separate_windows(source, outputs, predictor, sample_rate=rate, channels=2)
    assert frames == len(original) and gain == 1
    assert len(calls) == 4 and max(calls) <= rate * 34
    vocals = np.fromfile(outputs["vocals"], "<f4").reshape(-1, 2)
    background = np.fromfile(outputs["background"], "<f4").reshape(-1, 2)
    np.testing.assert_allclose(vocals + background, original, atol=1e-7)


@pytest.mark.parametrize("failure", ["nan", "shape", "keys"])
def test_invalid_model_outputs_are_rejected(tmp_path, failure):
    source = tmp_path / "source.f32"
    np.ones((200, 2), dtype="<f4").tofile(source)

    def predictor(chunk):
        if failure == "nan":
            chunk[0, 0] = np.nan
        return {} if failure == "keys" else {"background": chunk[:, :-1] if failure == "shape" else chunk}

    with pytest.raises(VocalSeparatorError):
        separate_windows(source, {"background": tmp_path / "out.f32"}, predictor,
                         sample_rate=100, channels=2)


def test_stem_gain_is_one_global_value_for_all_windows(tmp_path):
    source = tmp_path / "source.f32"
    original = np.ones((6200, 2), dtype="<f4")
    original[3000:6000] *= 4
    original.tofile(source)
    outputs = {name: tmp_path / (name + ".f32") for name in ("vocals", "background")}
    frames, gain = separate_windows(
        source, outputs, lambda chunk: {"vocals": chunk, "background": chunk * .5},
        sample_rate=100, channels=2,
    )
    assert frames == 6200 and gain == pytest.approx(.245)
    result = np.fromfile(outputs["vocals"], "<f4").reshape(-1, 2) * gain
    assert result.max() == pytest.approx(.98)
    assert result[0, 0] / result[3000, 0] == .25


@pytest.mark.parametrize("silent", [False, True])
def test_demucs_adapter_saves_complete_finite_stems_without_downloading(tmp_path, monkeypatch, silent):
    # Inference is an explicit array oracle. Actual FFmpeg decode/encode and adapter are exercised.
    calls = []
    repo = tmp_path / "models"
    repo.mkdir()

    class Tensor:
        def __init__(self, array):
            self.array = array

        def __getitem__(self, key):
            return Tensor(self.array[key])

        def cpu(self):
            return self

        def numpy(self):
            return self.array

    class Model:
        sources = ("vocals", "other")
        samplerate = 16000
        audio_channels = 2

        def to(self, device):
            assert device == "cpu"

    def get_model(name, *, repo):
        calls.append(("model", name, repo))
        return Model()

    def apply_model(model, mix, **kwargs):
        calls.append(("inference", mix.array.shape))
        return Tensor(np.stack([mix.array * .25, mix.array * .75], axis=1))

    torch = SimpleNamespace(from_numpy=Tensor, inference_mode=nullcontext,
                            cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "demucs", ModuleType("demucs"))
    monkeypatch.setitem(sys.modules, "demucs.pretrained", SimpleNamespace(get_model=get_model))
    monkeypatch.setitem(sys.modules, "demucs.apply", SimpleNamespace(apply_model=apply_model))
    monkeypatch.setattr(separator, "is_demucs_available", lambda: True)
    source = (write_wave(tmp_path / "source.wav", np.zeros((44100, 2))) if silent
              else stereo_fixture(tmp_path / "source.wav"))
    output = tmp_path / "out"
    result = separator.separate_vocals(source, output, model_repo=repo)
    assert calls[0] == ("model", "htdemucs", repo)
    assert len(calls) == (1 if silent else 2)
    assert set(result["stems"]) == {"vocals", "background"}
    for path in result["stems"].values():
        stats = wave_stats(path)
        assert stats["frames"] == 16000
        assert stats["peak"] <= .981
        assert np.isfinite(read_wave(path)).all()
    assert len(list(output.iterdir())) == 1


def test_supervisor_publishes_real_effect_with_manifest(tmp_path):
    source = stereo_fixture(tmp_path / "source.wav")
    output = tmp_path / "output"
    result = supervisor.run_vocal_separation_job(source, output, prefer_demucs=False)
    path = Path(result["stems"]["background"])
    assert path.is_file() and path.is_relative_to(output)
    assert json.loads((path.parent / "result.json").read_text(encoding="utf-8")) == result
    assert wave_stats(path)["mono_rms"] > .1
    assert not list(output.glob(".separation-job-*"))


@pytest.mark.parametrize("cancel", [False, True])
def test_supervisor_reclaims_stuck_worker_and_its_child(tmp_path, monkeypatch, cancel):
    from test_subtitle_asr_worker import is_running

    observed = {}

    def fixture_process(command, **kwargs):
        root = Path(command[-1])
        observed["root"] = root
        script = """
import pathlib,subprocess,sys,time
root=pathlib.Path(sys.argv[1])
while not (root/'ready').exists():time.sleep(.02)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
(root/'descendant.pid').write_text(str(child.pid))
time.sleep(60)
"""
        child = OwnedProcess([sys.executable, "-c", script, str(root)], **kwargs)
        observed["pid"] = child.process.pid
        return child

    class Context:
        def raise_if_canceled(self):
            marker = observed.get("root", tmp_path) / "descendant.pid"
            if marker.exists():
                observed["descendant"] = int(marker.read_text())
                if cancel:
                    raise SubtitleJobCanceled("cancel fixture")

    monkeypatch.setattr(supervisor, "OwnedProcess", fixture_process)
    started = time.monotonic()
    with pytest.raises(SubtitleJobCanceled if cancel else VocalSeparatorError):
        supervisor.run_vocal_separation_job(tmp_path / "source.wav", tmp_path / "output",
                                            context=Context(), timeout_seconds=2)
    assert time.monotonic() - started < 8
    assert not is_running(observed["pid"])
    assert not is_running(observed["descendant"])
    assert not list((tmp_path / "output").iterdir())
