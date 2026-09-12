"""Portable local/Kaggle runner. No dependency on the Content Bot backend."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import time
from pathlib import Path

SDK = "3.6.4"
MODEL = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
REVISION = "8b7e9cffb4b41918cb638b9f62f0a751184d14a6"
V2_MODEL = "pnnbao-ump/VieNeu-TTS-v2-Turbo"
V2_REVISION = "afe400abff18c00b52b246bb4d21f02a86855eb7"
V2_CODEC = "pnnbao-ump/VieNeu-Codec"
ENGINES = {"v3turbo": (MODEL, REVISION), "v2turbo": (V2_MODEL, V2_REVISION)}
PINS = {
    MODEL: REVISION,
    V2_MODEL: V2_REVISION,
    V2_CODEC: "eee9889a4176270272a07395c6540e06f9312184",
    "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX": "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae",
    "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano": "6aa02b01e445cc585582cf0ba480bc3ea6c8dd68",
}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    part.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):
        try:
            part.replace(path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


class V2Engine:
    """Adapt the v2 speaker encoder/decoder to the portable worker interface."""

    def __init__(self, engine, denoiser_path):
        self.engine = engine
        self.sample_rate = engine.sample_rate
        self.denoiser_path = denoiser_path
        self.voices = {}

    def list_preset_voices(self):
        return self.engine.list_preset_voices()

    def add_voice(self, name, reference, *, denoise=True, save=False):
        import numpy as np
        from vieneu.v3turbo import V3TurboVieNeuTTS
        clean = V3TurboVieNeuTTS._preclean_reference_audio(reference)
        try:
            if denoise:
                import soundfile as sf
                import soxr
                from vieneu._v3_turbo_engine.onnx_denoiser import OnnxDenoiser
                wav, sr = sf.read(clean, dtype="float32")
                wav = OnnxDenoiser(str(self.denoiser_path)).denoise(wav, sr)
                codes = self.engine.encode_reference(soxr.resample(wav, 44100, 24000))
            else:
                codes = self.engine.encode_reference(clean)
            codes = np.asarray(codes, dtype=np.float32)
            if codes.shape != (1, 128) or not np.isfinite(codes).all():
                raise ValueError("V2 không trích được đặc trưng giọng hợp lệ.")
            self.voices[name] = codes
        finally:
            if Path(clean).resolve() != Path(reference).resolve():
                Path(clean).unlink(missing_ok=True)

    def infer(self, text, *, voice=None, temperature=0.8, batch_size=1):
        data = self.voices.get(voice) if isinstance(voice, str) else None
        if data is None:
            data = self.engine.get_preset_voice(None if voice == "__default__" else voice)
        return self.engine.infer(text, voice=data, temperature=0.4, show_progress=False)


def load_v2_engine(device, cache, download):
    if device != "cuda":
        raise ValueError("Engine V2 Turbo cần NVIDIA GPU trong ứng dụng này.")
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA chưa sẵn sàng cho V2 Turbo.")
    from huggingface_hub import snapshot_download
    from vieneu import Vieneu
    snapshots = {}
    for repo, patterns in [(V2_MODEL, ["*.json", "*.txt", "*.safetensors"]),
                           (V2_CODEC, ["vieneu_encoder.onnx", "vieneu_decoder.onnx"]),
                           (MODEL, ["denoiser.onnx"])]:
        snapshots[repo] = Path(snapshot_download(
            repo, revision=PINS[repo], cache_dir=str(cache), allow_patterns=patterns,
            local_files_only=not download, max_workers=2))
    engine = Vieneu(mode="turbo_gpu", backbone_repo=str(snapshots[V2_MODEL]),
                    decoder_repo=str(snapshots[V2_CODEC] / "vieneu_decoder.onnx"),
                    encoder_repo=str(snapshots[V2_CODEC] / "vieneu_encoder.onnx"),
                    device=device, backend="standard")
    if engine.encoder_sess is None:
        raise RuntimeError("Thiếu bộ mã hóa mẫu giọng V2 Turbo.")
    return V2Engine(engine, snapshots[MODEL] / "denoiser.onnx")


def load_engine(device, *, download=False, model_id=MODEL):
    if importlib.metadata.version("vieneu") != SDK:
        raise RuntimeError(f"Cần vieneu=={SDK}; môi trường hiện tại khác phiên bản.")
    import huggingface_hub as hub
    if os.name == "nt":
        # Windows standard-user sessions cannot reliably create symlinks.
        # Force the Hub's supported copy fallback for every cache directory.
        import huggingface_hub.file_download as hub_files
        hub_files.are_symlinks_supported = lambda cache_dir=None: False
    cache = Path(os.environ.get("CONTENT_BOT_VOICE_MODEL_DIR", Path(__file__).parent / "models"))
    if model_id == V2_MODEL:
        return load_v2_engine(device, cache, download)
    if model_id != MODEL:
        raise ValueError("Engine giọng đọc không được hỗ trợ.")
    required = [MODEL, "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"]
    if device == "cuda":
        required.append("OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano")
    snapshots = {}
    for repo in required:
        patterns = ["*.json", "speaker_encoder.onnx", "denoiser.onnx", "onnx_update/*"] if repo == MODEL else None
        if repo == MODEL and device == "cuda":
            patterns += ["update/*"]
        snapshots[repo] = Path(hub.snapshot_download(repo, revision=PINS[repo], cache_dir=str(cache),
                                                    allow_patterns=patterns, local_files_only=not download,
                                                    max_workers=2))
    original = hub.hf_hub_download

    def pinned_download(repo_id, filename, *args, **kwargs):
        if repo_id in snapshots:
            candidate = snapshots[repo_id] / (kwargs.get("subfolder") or "") / filename
            if candidate.is_file():
                return str(candidate)
            raise FileNotFoundError(f"Thiếu artifact đã khóa: {repo_id}/{filename}")
        raise ValueError(f"Model chưa có trong danh sách phiên bản đã khóa: {repo_id}")

    hub.hf_hub_download = pinned_download
    # Runtime library imports the hub helper lazily. Keep the pin in place for cloning too.
    from vieneu import Vieneu
    kwargs = dict(mode="v3turbo", backbone_repo=str(snapshots[MODEL]), device=device,
                  backend="onnx" if device == "cpu" else "pytorch", precision="fp32",
                  max_batch_size=1)
    if device == "cpu":
        kwargs["onnx_dir"] = str(snapshots[MODEL] / "onnx_update")
    else:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA chưa sẵn sàng. Cài bản GPU hoặc chọn CPU.")
        kwargs["moss_tokenizer"] = str(snapshots["OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano"])
    try:
        return Vieneu(**kwargs)
    except Exception:
        hub.hf_hub_download = original
        raise


def prepare(device, engine_id="v3turbo"):
    model_id, revision = ENGINES[engine_id]
    path = Path(__file__).parent / ("runtime-status.json" if engine_id == "v3turbo" else "runtime-status-v2turbo.json")
    try:
        prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(prior, dict):
            prior = {}
    except (ValueError, OSError):
        prior = {}
    engine = load_engine(device, download=True, **({"model_id": model_id} if model_id != MODEL else {}))
    import soundfile as sf
    audio = engine.infer("Xin chào, đây là giọng đọc thử cho video của bạn.",
                         voice="Ngọc Huyền" if model_id == MODEL else None, temperature=0.8, batch_size=1)
    import numpy as np
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not audio.size or not np.isfinite(audio).all() or not np.any(np.abs(audio) > 0.0001):
        raise ValueError("Mẫu kiểm tra không có lời đọc hợp lệ; chưa thể đánh dấu runtime sẵn sàng.")
    probe = Path(__file__).parent / ("probe.wav" if engine_id == "v3turbo" else "probe-v2turbo.wav")
    sf.write(str(probe), audio, engine.sample_rate, subtype="PCM_16")
    # Exercise the reference encoder as well as preset synthesis before declaring readiness.
    if model_id == V2_MODEL:
        engine.add_voice("setup-reference", str(probe), denoise=False, save=False)
    write(path, {"ready": True, "sdk_version": SDK, "model_revision": revision,
                 "presets": [{"name": label, "id": identifier} for label, identifier in engine.list_preset_voices()],
                 "devices": sorted(set(prior.get("devices", []) + [device])),
                 "message": "Model local đã sẵn sàng."})


def infer_with_retry(engine, text, voice_args, device):
    for attempt in range(2):
        try:
            return engine.infer(text, **voice_args, temperature=0.8, batch_size=1)
        except RuntimeError as exc:
            if device != "cuda" or "out of memory" not in str(exc).lower():
                raise
            if attempt:
                raise RuntimeError(
                    "GPU không đủ bộ nhớ dù đã thử lại với batch 1. "
                    "Các đoạn đã lưu vẫn còn; hãy chọn CPU hoặc xuất gói Kaggle."
                ) from exc
        # Clear outside the except block so the traceback no longer retains
        # intermediate tensors from the failed inference.
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()


def voice_arguments(engine, profile, root):
    if not profile.get("reference_id"):
        return {"voice": profile.get("preset")}
    reference = Path(root) / "reference.wav"
    if hashlib.sha256(reference.read_bytes()).hexdigest() != profile["reference_id"]:
        raise ValueError("Mẫu giọng bị thay đổi.")
    engine.add_voice("content-bot-reference", str(reference),
                     denoise=profile.get("denoise", True), save=False)
    return {"voice": "content-bot-reference"}


def run(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    model_id = manifest["profile"].get("model_id", MODEL)
    revision = {MODEL: REVISION, V2_MODEL: V2_REVISION}.get(model_id)
    if (manifest.get("schema_version") != 1 or manifest.get("sdk_version") != SDK
            or revision is None or manifest["profile"]["model_revision"] != revision):
        raise ValueError("Gói tác vụ khác phiên bản worker/model.")
    progress = {"message": "Đang nạp model", "stage": "loading", "clip_id": None, "completed": [], "failed": []}
    if (root / "control.json").exists():
        progress["message"] = "Đã dừng theo yêu cầu"
        write(root / "progress.json", progress)
        return
    write(root / "progress.json", progress)
    engine = load_engine(manifest["device"], download=os.environ.get("VOICE_ALLOW_DOWNLOAD") == "1",
                         **({"model_id": model_id} if model_id != MODEL else {}))
    import numpy as np
    import soundfile as sf
    output = root / "assets"
    output.mkdir(exist_ok=True)
    voice_args = voice_arguments(engine, manifest["profile"], root)
    for item in manifest["clips"]:
        control = root / "control.json"
        if control.exists():
            progress["message"] = "Đã dừng theo yêu cầu"
            write(root / "progress.json", progress)
            return
        key = item["generation_hash"]
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid generation hash")
        path = output / f"{key}.wav"
        sidecar = output / f"{key}.json"
        if path.exists() and sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                info = sf.info(str(path))
                if (meta["checksum"] == hashlib.sha256(path.read_bytes()).hexdigest()
                        and meta["generation_hash"] == key and info.frames > 0
                        and info.samplerate == engine.sample_rate and info.channels == 1):
                    progress["completed"].append(item["id"])
                    write(root / "progress.json", progress)
                    continue
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                # A damaged checkpoint is regenerated, not a reason to lose
                # the rest of a long local or Kaggle run.
                pass
        started = time.monotonic()
        progress["message"] = f"Đang tạo đoạn {len(progress['completed']) + 1}/{len(manifest['clips'])}"
        progress.update(stage="generating", clip_id=item["id"])
        write(root / "progress.json", progress)
        try:
            audio = infer_with_retry(engine, item["text"], voice_args, manifest["device"])
            audio = np.asarray(audio, dtype=np.float32).reshape(-1)
            if not audio.size or not np.isfinite(audio).all() or not np.any(np.abs(audio) > 0.0001):
                raise ValueError("Model không trả lời đọc hợp lệ.")
            # Leave headroom without amplifying noise or normalizing each line to peak 0 dB.
            peak = float(np.max(np.abs(audio)))
            if peak > 0.95:
                audio *= 0.95 / peak
            temporary = path.with_suffix(".part")
            sf.write(str(temporary), audio, engine.sample_rate, subtype="PCM_16", format="WAV")
            temporary.replace(path)
            write(sidecar, {"checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "generation_hash": key, "clip_id": item["id"],
                            "duration_ms": round(len(audio) / engine.sample_rate * 1000),
                            "elapsed_seconds": round(time.monotonic() - started, 2)})
            progress["completed"].append(item["id"])
        except Exception as exc:
            progress["failed"].append({"clip_id": item["id"], "error": str(exc)[:1000]})
        write(root / "progress.json", progress)
    progress["message"] = "Đã xử lý xong" if not progress["failed"] else "Một số đoạn cần thử lại"
    progress.update(stage="finished", clip_id=None)
    write(root / "progress.json", progress)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "run"])
    parser.add_argument("target", help="cpu/cuda for prepare, work directory for run")
    parser.add_argument("--engine", choices=ENGINES, default="v3turbo")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.target, args.engine)
    else:
        run(args.target)
