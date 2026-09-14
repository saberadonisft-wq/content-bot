"""Compare CPU ONNX probabilities with the pinned upstream PyTorch wrapper.

Run in a verification environment with torch, torchaudio and onnxruntime.
These libraries are not required by the production streaming wrapper.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.gemini_media import VAD_MODEL, VAD_SHA256, VAD_VERSION, SileroStream

REFERENCE_SHA256 = "dbdcfc5cef2c14abf8cc29b44afb79038509a9bc34ae8ba2e7482b48813949ee"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = VAD_MODEL.parent / "reference_utils_vad.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == REFERENCE_SHA256
    spec = importlib.util.spec_from_file_location("silero_reference", source)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    original = reference.OnnxWrapper(str(VAD_MODEL), force_onnx_cpu=True)
    current = SileroStream()
    with wave.open(str(args.audio), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        sample = np.frombuffer(audio.readframes(16000 * 20 + 137), dtype="<i2").astype(np.float32) / 32768
    signals = {"real_audio_with_remainder": sample,
               "silence_next_video": np.zeros(16000 + 1, dtype=np.float32),
               "noise_next_video": np.random.default_rng(7).normal(0, 0.1, 16000 + 137).astype(np.float32)}
    results = []
    for name, signal in signals.items():
        expected = original.audio_forward(torch.from_numpy(signal.copy()), 16000).numpy().reshape(-1)
        for block_size in (113, 16000, len(signal)):
            current.reset()
            observed = []
            for start in range(0, len(signal), block_size):
                observed.extend(current.feed(signal[start:start + block_size]))
            observed.extend(current.feed([], final=True))
            values = np.array([p for _, p in observed])
            error = float(np.max(np.abs(expected - values)))
            assert len(values) == len(expected)
            assert error <= 1e-6, (name, block_size, error)
            results.append({"signal": name, "read_samples": block_size, "frames": len(values), "max_absolute_error": error})
    report = {"vad_version": VAD_VERSION, "model_sha256": VAD_SHA256,
              "reference_sha256": REFERENCE_SHA256, "torch": torch.__version__,
              "comparison": "ONNX probabilities and stream resets; not recognition/timing accuracy", "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
