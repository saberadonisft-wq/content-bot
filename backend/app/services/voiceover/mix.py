"""Compose a bounded-memory source-time voice track, then let the video renderer cut it."""

from __future__ import annotations

import array
import math
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import ExitStack
from pathlib import Path

import imageio_ffmpeg

from ..subtitle_render import SubtitleRenderCanceled, _effective_video_segments
from .audio import audio_metadata
from .dsp_cache import prepare_clip_dsp
from .models import VoiceDocument
from .separation_store import SeparationStore, SeparationStoreError
from .store import VoiceStore, digest, generation_hash, read_json, write_json
from .timing import file_interval, window_issues

SAMPLE_RATE = 48000


def write_duck_envelope(path, doc, duration_ms, cancel=None):
    """1 kHz control track: 20 ms attack, 250 ms release, floor 0.25.

    Matches preview's timeline envelope, including silence within a spoken clip.
    Stream chunks instead of expanding a long video's filter graph or RAM use.
    """
    intervals = sorted((c.start_ms + c.offset_ms,
                        c.start_ms + c.offset_ms + c.duration_ms / c.rate)
                       for c in doc.clips if c.asset_id and c.gain > 0)
    index = 0
    active = []
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 1000, 0, 'NONE', 'not compressed'))
        for lower in range(0, duration_ms, 5000):
            if cancel and cancel.is_set():
                raise SubtitleRenderCanceled('Đã hủy trộn giọng đọc.')
            samples = array.array('h')
            for ms in range(lower, min(lower + 5000, duration_ms)):
                while index < len(intervals) and intervals[index][0] <= ms:
                    active.append(intervals[index]); index += 1
                active = [(start, end) for start, end in active if ms < end + 250]
                factor = 1.0
                for start, end in active:
                    depth = min(1, (ms - start) / 20, max(0, (end + 250 - ms) / 250))
                    factor = min(factor, 1 - 0.75 * depth)
                samples.append(round(factor * 32767))
            if sys.byteorder != 'little':
                samples.byteswap()
            output.writeframes(samples.tobytes())


def map_voice_document(doc, options, duration):
    segments, _ = _effective_video_segments({'duration_ms': duration}, options)
    speed = float(options.get('video_speed', 1))
    if not 0.5 <= speed <= 2:
        raise ValueError('Tốc độ video phải từ 0.5 đến 2.')
    verify_voice_cuts(doc, options, duration)
    mapped, offset = [], 0
    for lower, upper in segments:
        for clip in doc.clips:
            start = clip.start_ms + clip.offset_ms
            if lower <= start < upper:
                mapped_start = round((offset + start - lower) / speed)
                mapped_end = mapped_start + round((clip.end_ms - clip.start_ms) / speed)
                sync = clip.sync.model_copy(deep=True)
                if sync.alignment:
                    # Source proof was validated before mapping. This private mix timeline
                    # contains file extents; subtitle display timing is handled separately.
                    mapped_end = max(mapped_end, mapped_start + math.ceil(sync.alignment.output_duration_ms / speed))
                    sync.alignment = None
                mapped.append(clip.model_copy(update={
                    'start_ms': mapped_start, 'offset_ms': 0,
                    'end_ms': mapped_end, 'sync': sync,
                    'rate': clip.rate * speed,
                }))
        offset += upper - lower
    return doc.model_copy(update={'clips': mapped}), round(offset / speed), segments, speed


def tempo_filter(rate: float) -> str:
    filters = []
    while rate < 0.5:
        filters.append("atempo=0.5")
        rate *= 2
    while rate > 2:
        filters.append("atempo=2")
        rate /= 2
    if rate != 1:
        filters.append(f"atempo={rate}")
    return ",".join(filters) + ("," if filters else "")


def retained_voice_document(doc, options, duration_ms):
    """Ignore wholly removed speech; unknown audio conservatively uses its cue window."""
    segments, _ = _effective_video_segments({'duration_ms': duration_ms}, options)
    clips = []
    for clip in doc.clips:
        start = clip.start_ms + clip.offset_ms
        end = start + (clip.duration_ms / clip.rate if clip.duration_ms else clip.end_ms - clip.start_ms)
        if any(start < upper and end > lower for lower, upper in segments):
            clips.append(clip)
    return doc.model_copy(update={'clips': clips})


def export_voice_audio(
    store: VoiceStore, owner: str, doc: VoiceDocument, audio_format: str,
    options: dict | None = None, duration_ms: int | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    if cancel and cancel.is_set():
        raise SubtitleRenderCanceled("Đã hủy xuất giọng đọc.")
    if audio_format not in {"wav", "flac", "mp3"}:
        raise ValueError("Định dạng audio không được hỗ trợ.")
    duration = duration_ms or max((c.end_ms + c.offset_ms for c in doc.clips), default=0)
    if not duration:
        raise ValueError("Chưa có đoạn giọng để xuất.")
    options = options or {}
    doc = retained_voice_document(doc, options, duration)
    mapped_doc, mapped_duration, segments, speed = map_voice_document(doc, options, duration)
    source = compose_voice(store, owner, mapped_doc, mapped_duration, cancel)
    gain = 0 if doc.mix.muted else doc.mix.gain
    key = digest({"source": source.stem, "gain": gain, "format": audio_format, "segments": segments, "speed": speed})
    final = source.parent / f"export-{key}.{audio_format}"
    if final.exists():
        return final
    codec = {
        "wav": ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    }[audio_format]
    with tempfile.TemporaryDirectory(dir=source.parent, prefix="export-") as tmp:
        output = Path(tmp) / f"audio.{audio_format}"
        ffmpeg(
            [
                "-i",
                str(source),
                "-af",
                f"volume={gain},alimiter=limit=0.95:level=false:latency=true",
                *codec,
                str(output),
            ], cancel
        )
        output.replace(final)
    return final


def ffmpeg(args: list[str], cancel: threading.Event | None = None, timeout=7200):
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-nostdin", "-y", *args],
            stdout=subprocess.DEVNULL,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        start = time.monotonic()
        try:
            while proc.poll() is None:
                if cancel and cancel.is_set():
                    raise SubtitleRenderCanceled("Đã hủy xử lý âm thanh.")
                if time.monotonic() - start > timeout:
                    raise TimeoutError("Trộn âm thanh quá thời gian chờ.")
                time.sleep(0.1)
            if proc.returncode:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 2000))
                raise ValueError(log.read().decode(errors="replace"))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def verify_document(
    store: VoiceStore, owner: str, doc: VoiceDocument, duration_ms: int,
    cancel: threading.Event | None = None,
    snapshot: dict | None = None,
) -> dict:
    store.validate_alignments(owner, doc)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    assets_snapshot = snapshot.setdefault("assets", {})
    last_end = 0
    for clip in sorted(doc.clips, key=lambda c: c.start_ms + c.offset_ms):
        if cancel and cancel.is_set():
            raise SubtitleRenderCanceled('Đã hủy kiểm tra giọng đọc.')
        if not clip.asset_id:
            raise ValueError(
                "Còn đoạn chưa tạo giọng. Tạo phần còn thiếu trước khi xuất."
            )
        meta = read_json(store.path(owner, "assets", clip.asset_id))
        audio_path = store.path(owner, "assets", clip.asset_id, ".wav")
        stat = audio_path.stat()
        cached = assets_snapshot.get(clip.asset_id)
        measured = None
        if isinstance(cached, dict):
            cached_meta = cached.get("metadata")
            if (
                cached.get("size_bytes") == stat.st_size
                and cached.get("mtime_ns") == stat.st_mtime_ns
                and isinstance(cached_meta, dict)
                and cached_meta.get("checksum") == meta.get("checksum")
            ):
                measured = cached_meta
        if measured is None:
            measured = audio_metadata(audio_path)
            assets_snapshot[clip.asset_id] = {
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "metadata": measured,
            }
        if measured["checksum"] != meta["checksum"]:
            raise ValueError(
                "Audio bị thay đổi hoặc hỏng. Tạo lại đoạn giọng trước khi xuất."
            )
        if measured['duration_ms'] != meta['duration_ms'] or clip.duration_ms != measured['duration_ms']:
            raise ValueError('Thời lượng audio không khớp dữ liệu đã lưu. Tải lại hoặc tạo lại đoạn giọng trước khi xuất.')
        if generation_hash(doc, clip, meta["device"]) != meta["generation_hash"]:
            raise ValueError(
                "Lời đọc đã thay đổi. Tạo lại các đoạn cần cập nhật trước khi xuất."
            )
        if clip.error:
            raise ValueError(clip.error)
        start, end = file_interval(clip)
        if start < last_end - 2 or end > duration_ms + 2:
            raise ValueError(
                "Các đoạn giọng chồng nhau hoặc vượt video. Chỉnh mốc/tốc độ trước khi xuất."
            )
        if window_issues(clip):
            raise ValueError(
                "Có đoạn giọng lệch khỏi khung phụ đề (kể cả độ dịch). Chỉnh mốc hoặc xác minh vùng nói trước khi xuất."
            )
        last_end = end
    return snapshot


def verify_voice_cuts(doc: VoiceDocument, options: dict, duration_ms: int):
    """Allow fully removed lines, but reject edits that cut through spoken audio."""
    kept, _ = _effective_video_segments({"duration_ms": duration_ms}, options)
    for clip in doc.clips:
        start = clip.start_ms + clip.offset_ms
        end = start + clip.duration_ms / clip.rate
        overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in kept if b > a)
        if overlap > 2 and overlap < end - start - 2:
            raise ValueError(
                f"Điểm cắt đi qua lời đọc: {clip.spoken_text[:80]}. Chỉnh đoạn giọng hoặc điểm cắt trước khi xuất."
            )


def compose_voice(
    store: VoiceStore,
    owner: str,
    doc: VoiceDocument,
    duration_ms: int,
    cancel: threading.Event | None = None,
    verification_snapshot: dict | None = None,
) -> Path:
    if cancel and cancel.is_set():
        raise SubtitleRenderCanceled('Đã hủy ghép âm thanh.')
    key = digest({"pipeline": 3, "duration": duration_ms, "clips": [
        {"asset": clip.asset_id, "start": clip.start_ms + clip.offset_ms,
         "rate": clip.rate, "gain": clip.gain}
        for clip in sorted(doc.clips, key=lambda c: c.start_ms + c.offset_ms)
    ]})
    directory = store.owner_root(owner) / "mixes"
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{key}.wav"
    snapshot_path = directory / f"{key}.json"
    if verification_snapshot is None:
        try:
            candidate_snapshot = read_json(snapshot_path)
            if (
                candidate_snapshot.get("version") == 1
                and candidate_snapshot.get("mix_key") == key
                and isinstance(candidate_snapshot.get("assets"), dict)
            ):
                verification_snapshot = candidate_snapshot
            else:
                verification_snapshot = None
        except (OSError, AttributeError, ValueError, TypeError, KeyError):
            verification_snapshot = None
    verification_snapshot = verify_document(
        store,
        owner,
        doc,
        duration_ms,
        cancel,
        snapshot=verification_snapshot,
    )
    if final.exists():
        write_json(
            snapshot_path,
            {"version": 1, "mix_key": key, "assets": verification_snapshot["assets"]},
        )
        return final
    if (
        shutil.disk_usage(directory).free
        < duration_ms / 1000 * SAMPLE_RATE * 2 + 512 * 1024 * 1024
    ):
        raise ValueError("Không đủ dung lượng trống để ghép giọng đọc.")
    with tempfile.TemporaryDirectory(dir=directory, prefix="compose-") as tmp:
        tmp = Path(tmp)
        output = tmp / "track.wav"
        dsp_cache_dir = store.owner_root(owner) / "clip-dsp-cache"
        with ExitStack() as leases, wave.open(str(output), "wb") as wav:
            wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            position = 0

            def silence(frames):
                while frames > 0:
                    if cancel and cancel.is_set():
                        raise SubtitleRenderCanceled("Đã hủy ghép âm thanh.")
                    size = min(frames, SAMPLE_RATE * 5)
                    wav.writeframesraw(b"\0\0" * size)
                    frames -= size

            for clip in sorted(doc.clips, key=lambda c: c.start_ms + c.offset_ms):
                source = store.path(owner, "assets", clip.asset_id, ".wav")
                asset_meta = read_json(store.path(owner, 'assets', clip.asset_id))
                prepared = prepare_clip_dsp(
                    source,
                    checksum=asset_meta["checksum"],
                    rate=clip.rate,
                    gain=clip.gain,
                    fade_in_ms=0 if asset_meta.get("processing") else 3,
                    cache_dir=dsp_cache_dir,
                    cancel=cancel,
                    lease_stack=leases,
                )
                start = round((clip.start_ms + clip.offset_ms) * SAMPLE_RATE / 1000)
                if start < position:
                    raise ValueError(
                        "Audio thực tế chồng nhau sau khi đổi tốc độ. Cần chỉnh khoảng nghỉ."
                    )
                silence(start - position)
                with wave.open(str(prepared["path"]), "rb") as segment:
                    while block := segment.readframes(SAMPLE_RATE * 5):
                        if cancel and cancel.is_set():
                            raise SubtitleRenderCanceled('Đã hủy ghép âm thanh.')
                        wav.writeframesraw(block)
                    position = start + segment.getnframes()
            silence(max(0, round(duration_ms * SAMPLE_RATE / 1000) - position))
        output.replace(final)
    write_json(
        snapshot_path,
        {"version": 1, "mix_key": key, "assets": verification_snapshot["assets"]},
    )
    return final


def prepare_voiced_video(
    store: VoiceStore,
    owner: str,
    doc: VoiceDocument,
    source: Path,
    media: dict,
    source_gain: float,
    cancel=None,
    voice_source: Path | None = None,
    background_segments: list[tuple[int, int]] | None = None,
    background_speed: float = 1,
) -> Path:
    voice = voice_source or compose_voice(store, owner, doc, media["duration_ms"], cancel)
    background = None
    if doc.mix.background_stem_id and doc.mix.mode != "voice":
        try:
            if doc.mix.background_source_fingerprint != doc.video_fingerprint:
                raise SeparationStoreError("Stem âm nền không thuộc video hiện tại.")
            background = SeparationStore(store.root / "separation-cache").file(
                owner, doc.mix.background_stem_id, "background"
            )
            manifest = SeparationStore(store.root / "separation-cache").get(
                owner, doc.mix.background_stem_id
            )
            if manifest["stems"]["background"]["checksum"] != doc.mix.background_stem_checksum:
                raise SeparationStoreError("Checksum stem âm nền đã thay đổi.")
        except (OSError, ValueError, SeparationStoreError) as exc:
            raise ValueError("Stem âm nền đã cũ hoặc không còn hợp lệ. Chọn lại stem trước khi xuất.") from exc
    key = digest(
        {
            "duck_pipeline": 2,
            "voice": voice.stem,
            "source": media["fingerprint"],
            "gain": source_gain,
            "mix": doc.mix.model_dump(),
            "background": str(background) if background else None,
            "background_segments": background_segments,
            "background_speed": background_speed,
        }
    )
    final = voice.parent / f"{key}.mkv"
    if final.exists():
        return final
    # Isolated temp directory permits simultaneous audio exports without shared .part files.
    with tempfile.TemporaryDirectory(dir=voice.parent, prefix="mux-") as tmp:
        output = Path(tmp) / "voiced.mkv"
        if background and background_segments:
            if not 0.5 <= background_speed <= 2:
                raise ValueError("Tốc độ âm nền không hợp lệ.")
            trimmed_background = Path(tmp) / "background.wav"
            filters = []
            for index, (lower, upper) in enumerate(background_segments):
                filters.append(
                    f"[0:a]atrim=start={lower / 1000:.6f}:end={upper / 1000:.6f},"
                    f"asetpts=PTS-STARTPTS[b{index}]"
                )
            labels = "".join(f"[b{index}]" for index in range(len(filters)))
            filter_graph = ";".join(filters) + f";{labels}concat=n={len(filters)}:v=0:a=1"
            if background_speed != 1:
                filter_graph += "," + tempo_filter(background_speed).rstrip(",")
            filter_graph += ",aresample=48000,pan=stereo|c0=c0|c1=c1"
            ffmpeg(
                ["-i", str(background), "-filter_complex", filter_graph, "-c:a", "pcm_s16le",
                 str(trimmed_background)],
                cancel,
            )
            background = trimmed_background
        gain = 0 if doc.mix.muted else doc.mix.gain
        extra_inputs = []
        voice_index = 2 if background else 1
        original_index = 1 if background else 0
        graph = f"[{voice_index}:a]volume={gain}[voice]"
        if (media.get("has_audio") or background) and doc.mix.mode != "voice":
            graph += f";[{original_index}:a]volume={source_gain * doc.mix.original_gain * doc.mix.background_gain}[original]"
            if doc.mix.mode == "duck" and gain > 0:
                envelope = Path(tmp) / 'duck.wav'
                write_duck_envelope(envelope, doc, round(media['duration_ms']), cancel)
                extra_inputs = ['-i', str(envelope)]
                envelope_index = 3 if background else 2
                graph += f';[{envelope_index}:a]aresample=48000[envelope];[original][envelope]amultiply[ducked];[ducked][voice]amix=inputs=2:normalize=0:duration=longest[mix]'
            else:
                graph += (
                    ";[original][voice]amix=inputs=2:normalize=0:duration=longest[mix]"
                )
        else:
            graph += ";[voice]anull[mix]"
        graph += ";[mix]alimiter=limit=0.95:level=false:latency=true[aout]"
        ffmpeg(
            [
                "-i", str(source),
                *( ["-i", str(background)] if background else [] ),
                "-i", str(voice),
                *extra_inputs,
                "-filter_complex",
                graph,
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "pcm_s16le",
                "-t",
                f"{media['duration_ms'] / 1000:.3f}",
                str(output),
            ],
            cancel,
        )
        output.replace(final)
    return final


def finalize_voiced_render(store, owner, doc, result, output_dir, source_media, options, source_gain, cancel=None):
    """Mix narration after video edits so global atempo cannot move its onsets."""
    raw_mix = doc.mix.model_copy(update={"gain": 1, "muted": False})
    voice = export_voice_audio(store, owner, doc.model_copy(update={"mix": raw_mix}),
                               "wav", options, source_media["duration_ms"], cancel)
    source = output_dir / result["output_filename"]
    media = {"duration_ms": result["duration_ms"], "fingerprint": source.stem,
             "has_audio": source_media.get("has_audio", False)}
    mapped_doc, _, segments, speed = map_voice_document(doc, options, source_media['duration_ms'])
    mixed = prepare_voiced_video(
        store, owner, mapped_doc, source, media, source_gain, cancel, voice,
        background_segments=segments, background_speed=speed,
    )
    key = digest({"pipeline": 1, "mixed": mixed.stem})[:12]
    filename = f"subtitled_{result['video_id']}_{key}.mp4"
    final = output_dir / filename
    cached = final.exists()
    if not cached:
        with tempfile.TemporaryDirectory(dir=output_dir, prefix="voice-final-") as tmp:
            part = Path(tmp) / "output.mp4"
            ffmpeg(["-i", str(mixed), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(part)], cancel)
            part.replace(final)
    return {**result, "output_filename": filename, "subtitled_video_url": f"/api/v1/subtitles/renders/{filename}",
            "output_size_bytes": final.stat().st_size, "audio_copied": False, "cache_hit": cached}
