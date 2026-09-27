"""Source-time manifests and streaming Silero inference (no PyTorch dependency)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VAD_VERSION = "silero-v6.2.1"
VAD_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
VAD_MODEL = Path(__file__).resolve().parents[1] / "assets" / "silero-vad-v6.2.1" / "silero_vad.onnx"
MANIFEST_VERSION = 1
PLANNER_VERSION = "speech-pause-v1"
EXTRACTION_VERSION = "source-time-pcm-v1"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        # Windows readers/antivirus may briefly deny replacement even after our
        # temporary writer is closed. Keep the old complete JSON until replace
        # succeeds; do not retry disk-full, invalid paths or permanent failures.
        for attempt in range(11):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == 10:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


def digest_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ChunkPolicy:
    target_ms: int = 120_000
    search_radius_ms: int = 20_000
    min_pause_ms: int = 800
    context_ms: int = 2_000
    max_chunk_ms: int = 600_000

    def __post_init__(self):
        if not (30_000 <= self.target_ms <= self.max_chunk_ms <= 1_800_000):
            raise ValueError("Độ dài mục tiêu/giới hạn đoạn không hợp lệ.")
        if not (0 <= self.search_radius_ms < self.target_ms and 100 <= self.min_pause_ms <= 10_000 and 0 <= self.context_ms <= 10_000):
            raise ValueError("Thiết lập khoảng nghỉ/ngữ cảnh không hợp lệ.")


class NoSpeechBoundary(ValueError):
    pass


def plan_chunks(duration_ms: int, speech: list[tuple[int, int]], policy: ChunkPolicy,
                *, fingerprint: Any, source_start_ms: int = 0) -> dict[str, Any]:
    if duration_ms <= 0:
        raise ValueError("Media duration must be positive")
    merged: list[list[int]] = []
    for start, end in sorted(speech):
        start, end = max(0, start), min(duration_ms, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pauses = []
    previous = 0
    for start, end in merged + [[duration_ms, duration_ms]]:
        if start - previous >= policy.min_pause_ms:
            pauses.append((previous, start))
        previous = end
    chunks = []
    start = 0
    while start < duration_ms:
        target = start + policy.target_ms
        reason = "media_end"
        if duration_ms - start <= min(policy.target_ms + policy.search_radius_ms, policy.max_chunk_ms):
            end = duration_ms
        else:
            low = start + policy.target_ms - policy.search_radius_ms
            high = min(duration_ms, start + policy.max_chunk_ms)
            candidates = []
            for left, right in pauses:
                # Keep 200ms padding inside the observed pause on both sides.
                a, b = max(low, left + 200), min(high, right - 200)
                if a <= b:
                    candidates.append(min(max(target, a), b))
            if candidates:
                near = [point for point in candidates if point <= target + policy.search_radius_ms]
                end = min(near or candidates, key=lambda point: abs(point - target))
                reason = "speech_pause" if near else "extended_speech_pause"
            elif duration_ms - start <= policy.max_chunk_ms:
                end = duration_ms
                reason = "continuous_speech_to_media_end"
            else:
                raise NoSpeechBoundary(f"Không tìm được khoảng nghỉ từ {start / 1000:.1f}s trước giới hạn {high / 1000:.1f}s. Tăng giới hạn đoạn hoặc xử lý vùng này riêng.")
        chunks.append({"chunk_id": f"chunk-{len(chunks) + 1:05d}", "index": len(chunks),
                       "core_start_ms": start, "core_end_ms": end,
                       "media_start_ms": max(0, start - policy.context_ms),
                       "media_end_ms": min(duration_ms, end + policy.context_ms),
                       "actual_offset_ms": 0, "boundary_reason": reason,
                       "needs_review": reason.startswith(("continuous", "extended"))})
        start = end
    manifest = {"version": MANIFEST_VERSION, "algorithm": PLANNER_VERSION,
                "vad_version": VAD_VERSION, "vad_sha256": VAD_SHA256,
                "fingerprint": fingerprint, "duration_ms": duration_ms,
                "source_start_ms": source_start_ms, "policy": asdict(policy), "chunks": chunks,
                "speech": merged}
    manifest["id"] = digest_json(manifest)
    return manifest


class SileroStream:
    """16 kHz mono float32; frame and recurrent context match pinned OnnxWrapper."""
    def __init__(self, model_path: Path = VAD_MODEL):
        import numpy as np
        import onnxruntime as ort
        if not model_path.is_file() or hashlib.sha256(model_path.read_bytes()).hexdigest() != VAD_SHA256:
            raise ValueError("Thiếu model Silero v6.2.1 hoặc checksum không khớp.")
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        if {item.name for item in self.session.get_inputs()} != {"input", "state", "sr"}:
            raise ValueError("Silero ONNX input contract mismatch")
        self.np = np
        self.reset()

    def reset(self):
        self.state = self.np.zeros((2, 1, 128), dtype=self.np.float32)
        self.context = self.np.zeros((1, 64), dtype=self.np.float32)
        self.pending = self.np.empty(0, dtype=self.np.float32)
        self.frames = 0

    def feed(self, samples, *, final=False):
        np = self.np
        data = np.concatenate((self.pending, np.asarray(samples, dtype=np.float32).reshape(-1)))
        if final and len(data) % 512:
            data = np.pad(data, (0, 512 - len(data) % 512))
        outputs = []
        consumed = len(data) // 512 * 512
        for offset in range(0, consumed, 512):
            frame = data[offset:offset + 512].reshape(1, 512)
            model_input = np.concatenate((self.context, frame), axis=1)
            probability, self.state = self.session.run(None, {"input": model_input, "state": self.state, "sr": np.array(16000, dtype=np.int64)})
            outputs.append((self.frames * 32, float(probability[0][0])))
            self.context = model_input[:, -64:].copy()
            self.frames += 1
        self.pending = data[consumed:].copy()
        return outputs


def extract_audio(video: Path, output: Path, *, duration_ms: int, cancel_event: threading.Event,
                  timeout_seconds: float = 1800) -> None:
    import imageio_ffmpeg
    # first_pts=0 inserts initial silence for late audio and trims negative samples.
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video),
           "-map", "0:a:0", "-vn", "-af", "aresample=16000:async=1:first_pts=0,apad",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-t", str(duration_ms / 1000), "-y", str(output)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            if cancel_event.is_set():
                raise RuntimeError("Đã hủy phân tích tiếng nói")
            if time.monotonic() >= deadline:
                raise RuntimeError("Trích audio quá thời gian cho phép")
            try:
                proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode:
            raise ValueError("Không thể trích audio cho VAD")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=3)


def detect_speech(audio: Path, *, cancel_event: threading.Event, progress=None) -> list[tuple[int, int]]:
    import numpy as np
    model = SileroStream()
    speech = []
    active = None
    silence_start = None
    with wave.open(str(audio), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
            raise ValueError("VAD requires mono PCM16 at 16kHz")
        duration_ms = round(source.getnframes() / 16)
        while True:
            if cancel_event.is_set():
                raise RuntimeError("Đã hủy phân tích tiếng nói")
            block = source.readframes(16000 * 10)
            samples = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768
            for timestamp, probability in model.feed(samples, final=not block):
                if probability >= 0.5:
                    silence_start = None
                    if active is None:
                        active = max(0, timestamp - 30)
                elif active is not None and probability < 0.35:
                    if silence_start is None:
                        silence_start = timestamp
                    if timestamp - silence_start >= 100:
                        speech.append((active, min(duration_ms, silence_start + 30)))
                        active = None
                        silence_start = None
            if progress:
                progress(min(1.0, source.tell() / max(1, source.getnframes())))
            if not block:
                break
        if active is not None:
            speech.append((active, duration_ms))
    return speech


def prepare_manifest(video: Path, media: dict[str, Any], cache_root: Path, policy: ChunkPolicy,
                     *, cancel_event: threading.Event, progress=None) -> dict[str, Any]:
    """Cache speech separately from policy so changing cut settings does not rerun VAD."""
    stat = video.stat()
    identity = {"fingerprint": media.get("fingerprint"), "audio_hash": media.get("audio_hash"),
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "duration_ms": media["duration_ms"], "has_audio": media.get("has_audio", True),
                "source_start_ms": media.get("source_start_ms", 0),
                "extraction": EXTRACTION_VERSION, "vad": VAD_VERSION, "sha256": VAD_SHA256}
    directory = cache_root / digest_json(identity)
    directory.mkdir(parents=True, exist_ok=True)
    speech_path = directory / "speech.json"
    duration_ms = int(media["duration_ms"])
    speech = None
    try:
        cached = json.loads(speech_path.read_text(encoding="utf-8"))
        candidate = cached["speech"]
        if cached["identity"] == identity and isinstance(candidate, list) and all(
            isinstance(pair, list) and len(pair) == 2 and all(type(n) is int for n in pair)
            and 0 <= pair[0] < pair[1] <= duration_ms for pair in candidate
        ):
            speech = candidate
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if cancel_event.is_set():
        raise RuntimeError("Đã hủy phân tích tiếng nói")
    if speech is None:
        if not media.get("has_audio", True):
            speech = []
        else:
            audio = directory / f"audio-{uuid.uuid4().hex}.wav"
            try:
                extract_audio(video, audio, duration_ms=duration_ms, cancel_event=cancel_event)
                speech = detect_speech(audio, cancel_event=cancel_event, progress=progress)
            finally:
                audio.unlink(missing_ok=True)
        if cancel_event.is_set():
            raise RuntimeError("Đã hủy phân tích tiếng nói")
        atomic_json(speech_path, {"identity": identity, "speech": speech})
    manifest = plan_chunks(duration_ms, speech, policy, fingerprint=identity,
                           source_start_ms=media.get("source_start_ms", 0))
    atomic_json(directory / f"manifest-{manifest['id']}.json", manifest)
    return manifest
