"""Portable local/Kaggle runner. No dependency on the Content Bot backend."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
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


@contextmanager
def worker_slot(root):
    """OS-owned lock: only one local model, released even after a crash."""
    lock_path = Path(os.environ.get("VOICE_WORKER_LOCK", Path(__file__).parent / ".worker.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        while True:
            if (Path(root) / "control.json").exists():
                yield False
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.2)
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def watch_supervisor(heartbeat, *, timeout=30, interval=1):
    """Exit even during inference if the managing backend disappears.

    Exported Kaggle workers have no supervisor and do not start this watchdog.
    Only atomic WAV + sidecar pairs are recovered after a forced exit.
    """
    stopped = threading.Event()

    def watch():
        while not stopped.wait(interval):
            try:
                expired = time.time() - Path(heartbeat).stat().st_mtime > timeout
            except OSError:
                expired = True
            if expired:
                os._exit(75)

    threading.Thread(target=watch, name="voice-supervisor", daemon=True).start()
    return stopped


def performance_profile():
    path = Path(os.environ.get("CONTENT_BOT_VOICE_PROFILE", Path(__file__).parent / "performance-profile.json"))
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        if profile.get("sdk") == SDK and profile.get("model_revision") == REVISION:
            return profile
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def configure_compute(device="cuda"):
    """Bound native pools before importing the model; preserve synthesis settings."""
    threads = min(2 if device == "cuda" else 4, os.cpu_count() or 1)
    try:
        threads = max(1, min(8, int(os.environ.get("CONTENT_BOT_VOICE_THREADS",
                                                 performance_profile().get(f"{device}_threads", threads)))))
    except (ValueError, TypeError):
        pass
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(threads)
    os.environ["OMP_WAIT_POLICY"] = "PASSIVE"
    if device == "cuda":
        import torch
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
    import onnxruntime as ort
    original = ort.InferenceSession

    class BoundedSession(original):
        def __init__(self, path_or_bytes, sess_options=None, *args, **kwargs):
            options = sess_options if sess_options is not None else ort.SessionOptions()
            options.intra_op_num_threads = min(options.intra_op_num_threads or threads, threads)
            options.inter_op_num_threads = 1
            for name in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
                try:
                    current = options.get_session_config_entry(name)
                except (RuntimeError, AttributeError):
                    current = None
                if current != "0":
                    options.add_session_config_entry(name, "0")
            super().__init__(path_or_bytes, options, *args, **kwargs)

    ort.InferenceSession = BoundedSession


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
    kwargs = {"mode": "v3turbo", "backbone_repo": str(snapshots[MODEL]), "device": device,
              "backend": "onnx" if device == "cpu" else "pytorch", "precision": "fp32",
              "max_batch_size": 1}
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


def batch_limit(device, model_id=MODEL):
    if device != "cuda" or model_id != MODEL:
        return 1
    try:
        return max(1, min(4, int(os.environ.get("CONTENT_BOT_VOICE_BATCH_SIZE",
                                               performance_profile().get("cuda_batch_size", 2)))))
    except (ValueError, TypeError):
        return 2


def infer_group(engine, items, voice_args, device):
    """Keep per-cue results; shrink failed batches and isolate individual errors."""
    limit = getattr(engine, "_content_bot_batch_limit", len(items))
    if len(items) > limit:
        return [result for offset in range(0, len(items), limit)
                for result in infer_group(engine, items[offset:offset + limit], voice_args, device)]
    if len(items) > 1:
        try:
            audios = engine.infer_batch([item["text"] for item in items],
                                       **voice_args, temperature=0.8, batch_size=len(items))
            if len(audios) != len(items):
                raise ValueError("Batch không trả đủ audio cho từng đoạn.")
            return [(audio, None) for audio in audios]
        except Exception as exc:
            logging.getLogger(__name__).warning("Batch %s failed; retrying smaller groups: %s", len(items), str(exc)[:500])
        # Release the traceback/tensors before retrying smaller groups.
        if device == "cuda":
            import gc

            import torch
            gc.collect()
            torch.cuda.empty_cache()
        middle = len(items) // 2
        engine._content_bot_batch_limit = middle
        return (infer_group(engine, items[:middle], voice_args, device)
                + infer_group(engine, items[middle:], voice_args, device))
    try:
        return [(infer_with_retry(engine, items[0]["text"], voice_args, device), None)]
    except Exception as exc:
        return [(None, str(exc)[:1000])]


def run(root):
    heartbeat = os.environ.get("VOICE_SUPERVISOR_HEARTBEAT")
    watchdog = watch_supervisor(heartbeat) if heartbeat else None
    try:
        write(Path(root) / "progress.json", {
            "stage": "waiting", "message": "Đang chờ lượt tạo giọng", "completed": [], "failed": [],
        })
        with worker_slot(root) as acquired:
            if acquired:
                manifest = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
                configure_compute(manifest["device"])
                _run(root)
    finally:
        if watchdog:
            watchdog.set()


def _run(root):
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
    import soundfile as sf
    output = root / "assets"
    output.mkdir(exist_ok=True)
    voice_args = voice_arguments(engine, manifest["profile"], root)
    def checkpoint_valid(item):
        key = item["generation_hash"]
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid generation hash")
        path = output / f"{key}.wav"
        sidecar = output / f"{key}.json"
        if path.exists() and sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                info = sf.info(str(path))
                return (meta["checksum"] == hashlib.sha256(path.read_bytes()).hexdigest()
                        and meta["generation_hash"] == key and info.frames > 0
                        and info.samplerate == engine.sample_rate and info.channels == 1)
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                pass
        return False

    limit = batch_limit(manifest["device"], model_id)
    items = manifest["clips"]
    for offset in range(0, len(items), limit):
        control = root / "control.json"
        if control.exists():
            progress["message"] = "Đã dừng theo yêu cầu"
            write(root / "progress.json", progress)
            return
        group = []
        by_key = {}
        for item in items[offset:offset + limit]:
            if checkpoint_valid(item):
                progress["completed"].append(item["id"])
            else:
                by_key.setdefault(item["generation_hash"], []).append(item)
        group = [duplicates[0] for duplicates in by_key.values()]
        if not group:
            write(root / "progress.json", progress)
            continue
        started = time.monotonic()
        progress["message"] = f"Đang tạo đoạn {len(progress['completed']) + 1}/{len(manifest['clips'])}"
        progress.update(stage="generating", clip_id=group[0]["id"], batch_size=len(group))
        write(root / "progress.json", progress)
        results = infer_group(engine, group, voice_args, manifest["device"])
        elapsed = time.monotonic() - started
        for item, (audio, error) in zip(group, results):
            key = item["generation_hash"]
            path = output / f"{key}.wav"
            sidecar = output / f"{key}.json"
            try:
                if error:
                    raise ValueError(error)
                commit_audio(audio, engine.sample_rate, path, sidecar, item, elapsed / len(group))
                progress["completed"].extend(duplicate["id"] for duplicate in by_key[key])
            except Exception as exc:
                progress["failed"].extend({"clip_id": duplicate["id"], "error": str(exc)[:1000]}
                                          for duplicate in by_key[key])
            write(root / "progress.json", progress)
    progress["message"] = "Đã xử lý xong" if not progress["failed"] else "Một số đoạn cần thử lại"
    progress.update(stage="finished", clip_id=None)
    write(root / "progress.json", progress)


def commit_audio(audio, sample_rate, path, sidecar, item, elapsed):
    import numpy as np
    import soundfile as sf

    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not audio.size or not np.isfinite(audio).all() or not np.any(np.abs(audio) > 0.0001):
        raise ValueError("Model không trả lời đọc hợp lệ.")
    # Leave headroom without amplifying noise or normalizing each line to peak 0 dB.
    peak = float(np.max(np.abs(audio)))
    if peak > 0.95:
        audio *= 0.95 / peak
    temporary = path.with_suffix(".part")
    sf.write(str(temporary), audio, sample_rate, subtype="PCM_16", format="WAV")
    temporary.replace(path)
    write(sidecar, {"checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "generation_hash": item["generation_hash"], "clip_id": item["id"],
                    "duration_ms": round(len(audio) / sample_rate * 1000),
                    "elapsed_seconds": round(elapsed, 2)})


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
