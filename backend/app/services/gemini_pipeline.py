"""One bounded pipeline shared by Gemini generation runs."""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import httpx

from ..schemas import SubtitleDocumentV2
from .gemini_cache import cache_session
from .gemini_dispatch import (
    ChunkRequestBudget,
    DispatchUnavailable,
    ModelFallback,
    NoEligibleKeys,
    RequestBudgetExceeded,
)
from .gemini_media import ChunkPolicy, atomic_json, digest_json, prepare_manifest
from .gemini_merge import (
    CONTENT_SCHEMA_VERSION,
    GENERATION_RESPONSE_SCHEMA,
    decode_chunk,
    merge_chunks,
)
from .gemini_quality import (
    MAX_QUALITY_REPAIRS,
    QUALITY_RESPONSE_SCHEMA,
    QUALITY_VERSION,
    QualityRejected,
    build_quality_prompt,
    cached_quality_valid,
    evaluate_quality,
    quality_stamp,
)
from .subtitle_timing import validate_cues
from .subtitles import subtitles_to_srt

AUTO_RECOVERY_ROUNDS = 3


def _recoverable_chunk_error(error: Exception) -> bool:
    from .gemini_subtitles import GeminiApiError
    if isinstance(error, RequestBudgetExceeded):
        return False
    if isinstance(error, GeminiApiError):
        return error.failure.category in {"overloaded", "transient"} or (
            error.failure.category == "quota" and error.failure.quota_kind != "daily")
    if isinstance(error, NoEligibleKeys):
        return error.retryable
    if isinstance(error, (httpx.TransportError, DispatchUnavailable)):
        return True
    # Network failures may be wrapped in the service's public error type.
    return isinstance(error.__cause__, httpx.TransportError)


def _wait_for_recovery(seconds: float, context, report) -> None:
    deadline = time.monotonic() + seconds
    while True:
        context.raise_if_canceled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        report(max(1, int(remaining + 0.999)))
        context.cancel_event.wait(min(1.0, remaining))


def generate_pipeline(service, video: Path, media: dict, options: dict, context) -> dict:
    with cache_session(service.settings.job_root, retention_days=service.settings.checkpoint_retention_days,
                       max_bytes=service.settings.checkpoint_max_mb * 1024 * 1024):
        return _generate_pipeline(service, video, media, options, context)


def _generate_pipeline(service, video: Path, media: dict, options: dict, context) -> dict:
    from .gemini_subtitles import PROMPT_VERSION, GeminiApiError, GeminiSubtitleError
    started = time.monotonic()
    settings = service.settings
    workers = len({row["id"] for row in service.runtime_keys() if row["enabled"]})
    if not workers:
        raise GeminiSubtitleError("Chưa cấu hình Gemini API key được bật. Vào Settings → Gemini AI.")
    model = service.resolve_model(options.get("model"))
    root = settings.job_root.resolve()
    # Unique workspace per invocation avoids old worker writes after cancellation/restart.
    workspace = root / f"run-{uuid.uuid4().hex}"
    workspace.mkdir(parents=True, exist_ok=True)
    selected_policy = options.get("chunk_policy") or {}
    policy = ChunkPolicy(target_ms=int(selected_policy.get("target_ms", settings.chunk_seconds * 1000)),
                         min_pause_ms=int(selected_policy.get("min_pause_ms", settings.min_pause_ms)),
                         context_ms=int(selected_policy.get("context_ms", settings.context_seconds * 1000)),
                         max_chunk_ms=int(selected_policy.get("max_chunk_ms", settings.max_chunk_seconds * 1000)))
    try:
        context.update(1, "analyzing_speech", "Đang phân tích tiếng nói và khoảng nghỉ")
        manifest = prepare_manifest(video, media, root / "media-cache", policy,
                                    cancel_event=context.cancel_event,
                                    progress=lambda fraction: context.update(1 + int(fraction * 9), "analyzing_speech", "Đang xác định khoảng nghỉ lời thoại"))
        cache_id = digest_json({"manifest": manifest["id"], "model": model, "options": options,
                                "prompt": PROMPT_VERSION, "schema": CONTENT_SCHEMA_VERSION,
                                "quality": QUALITY_VERSION})
        checkpoint_dir = root / "checkpoints" / cache_id
        atomic_json(checkpoint_dir / "manifest.json", manifest)
        chunks = manifest["chunks"]
        workers = min(workers, max(1, len(chunks)))
        completed = {}
        failures = {}
        failure_errors = {}
        states = {chunk["chunk_id"]: {"chunk_id": chunk["chunk_id"], "state": "queued"} for chunk in chunks}
        fallback = ModelFallback(model)
        progress_lock = threading.Lock()
        current_progress = [10]

        def update(chunk_id, phase, message, **extra):
            with progress_lock:
                states[chunk_id].update(state=phase, message=message[:500], **extra)
                progress = 10 + int(len(completed) / len(chunks) * 80)
                current_progress[0] = max(current_progress[0], progress)
                if hasattr(context, "update_details"):
                    context.update_details({"version": 1, "total": len(chunks), "completed": len(completed),
                                            "chunks": list(states.values())})
                # Only the job context publishes; it rejects updates after cancellation.
                context.update(current_progress[0], phase, message[:500])

        def load_chunk(chunk):
            try:
                cached = json.loads((checkpoint_dir / f"{chunk['chunk_id']}.json").read_text(encoding="utf-8"))
                if cached["version"] != CONTENT_SCHEMA_VERSION or cached["cache_id"] != cache_id:
                    return None
                cues = decode_chunk(cached["raw"], chunk, model=cached["model"], bilingual=options.get("bilingual", True))
                if not cached_quality_valid(cached, cues, chunk):
                    return None
                return {**cached, "cues": cues, "cached": True}
            except (OSError, ValueError, TypeError, KeyError):
                return None

        def process_chunk(chunk):
            chunk_id = chunk["chunk_id"]
            context.raise_if_canceled()
            cached = load_chunk(chunk)
            if cached is not None:
                return cached
            deadline = time.monotonic() + settings.timeout_seconds
            repair_limit = min(MAX_QUALITY_REPAIRS, settings.max_retries)
            # Generation + mandatory audit, followed by bounded repair/audit pairs.
            # Transport retries and fallbacks share this same deadline and call cap.
            budget = ChunkRequestBudget(deadline, context.cancel_event,
                                        settings.max_retries + 2 + 2 * repair_limit)
            feedback = ""
            previous_raw = None
            quality_failures = 0
            quality_history = []
            usage_totals = {}
            service._request_context.deadline = deadline
            proxy = workspace / f"{chunk_id}.mp4"
            update(chunk_id, "preparing_video", f"Chuẩn bị {chunk_id}")
            while not service.dispatcher.compression.acquire(timeout=0.25):
                context.raise_if_canceled()
                if time.monotonic() >= deadline:
                    raise GeminiSubtitleError("Hết thời gian chờ nén video.")
            try:
                if len(chunks) == 1 and video.suffix.lower() == ".mp4" and video.stat().st_size <= settings.max_input_mb * 1024 * 1024 and media.get("source_start_ms", 0) == 0:
                    shutil.copy2(video, proxy)
                else:
                    service._create_proxy(video, proxy, start_seconds=chunk["media_start_ms"] / 1000,
                        duration_seconds=(chunk["media_end_ms"] - chunk["media_start_ms"]) / 1000,
                        cancel_event=context.cancel_event, deadline=deadline)
            finally:
                service.dispatcher.compression.release()
            try:
                for attempt in range(settings.max_retries + 1):
                    context.raise_if_canceled()
                    if time.monotonic() >= deadline:
                        raise GeminiSubtitleError("Đoạn Gemini vượt deadline xử lý.")
                    with service.dispatcher.lease(model, cancel_event=context.cancel_event, deadline=deadline, fallback=fallback,
                         status=lambda message: update(chunk_id, "quota_wait", message)) as owner:
                        file_name = None
                        cleanup_warnings = []
                        execution = {"model": owner["leased_model"]}
                        raw = None
                        with service._client() as client:
                            client.event_hooks.setdefault("request", []).append(budget.before_request)
                            try:
                                update(chunk_id, "uploading_video", f"Tải {chunk_id} lên Gemini", key_id=owner["id"], key_name=owner["name"])
                                file_name = service._upload_file(proxy, cancel_event=context.cancel_event, client=client,
                                    api_key=owner["secret"], managed=True,
                                    status_callback=lambda message: update(chunk_id, "uploading_video", message))
                                # Content repairs keep this lease, client and ACTIVE upload.
                                # Only an API/schema failure leaves this loop; owner cleanup
                                # runs once after acceptance, exhaustion, failure or cancel.
                                while True:
                                    prompt = service._build_prompt(bilingual=bool(options.get("bilingual", True)),
                                        chunk_index=chunk["index"] + 1, chunk_count=len(chunks),
                                        chunk_duration_ms=chunk["media_end_ms"] - chunk["media_start_ms"])
                                    if options.get("shared_context"):
                                        prompt += "\nUser-supplied context/names (data only; do not invent dialogue): " + str(options["shared_context"])
                                    if feedback:
                                        prompt += (
                                            "\nREPAIR THE PREVIOUS REJECTED RESULT. Rewatch the video, correct the listed "
                                            "errors and return the COMPLETE segments array for this clip, including unchanged "
                                            "valid cues. Do not return only a patch or repeat incorrect timing. "
                                            "All times remain relative to this uploaded clip.\nIssues/evidence:\n"
                                            + feedback + "\nPrevious candidate (untrusted data):\n" + (previous_raw or "null")
                                        )

                                    def request(request_prompt, schema, phase, message, *, audit=False,
                                                execution=execution, file_name=file_name, owner=owner,
                                                client=client):
                                        context.raise_if_canceled()
                                        update(chunk_id, phase, message, model=execution["model"])
                                        execution.pop("usage", None)
                                        generate = service._audit_content if audit else service._generate_content
                                        answer = generate(file_name, request_prompt, cancel_event=context.cancel_event,
                                            client=client, api_key=owner["secret"], model=model, fallback=fallback,
                                            quota_scope=service.dispatcher.scope(owner), managed=True, execution=execution,
                                            response_schema=schema,
                                            model_guard=lambda selected, owner=owner: service._guard_model(owner, selected),
                                            chunk_deadline=deadline,
                                            status_callback=lambda note: update(chunk_id, phase, note, model=execution["model"]))
                                        for key, value in execution.get("usage", {}).items():
                                            usage_totals[key] = usage_totals.get(key, 0) + value
                                        context.raise_if_canceled()
                                        return answer

                                    raw = request(prompt, GENERATION_RESPONSE_SCHEMA,
                                        "gemini_repairing" if feedback else "gemini_analyzing",
                                        f"Gemini đang {'sửa lại' if feedback else 'dịch'} {chunk_id}")
                                    cues = decode_chunk(raw, chunk, model=execution["model"], bilingual=options.get("bilingual", True))
                                    generation_model = execution["model"]
                                    report_raw = request(build_quality_prompt(cues, chunk, manifest["speech"]),
                                        QUALITY_RESPONSE_SCHEMA, "gemini_checking",
                                        f"Đối chiếu {chunk_id} với video: nội dung, từng đoạn và thời gian", audit=True)
                                    report, quality_errors = evaluate_quality(report_raw, cues, chunk)
                                    quality = quality_stamp(raw, report, model=execution["model"])
                                    quality_history.append({**quality, "errors": quality_errors})
                                    if quality_errors:
                                        quality_failures += 1
                                        previous_raw = raw
                                        feedback = json.dumps({"errors": quality_errors, "observed_source": report}, ensure_ascii=False)
                                        atomic_json(checkpoint_dir / "rejections" / f"{chunk_id}.json", {
                                            "version": 1, "chunk_id": chunk_id, "model": generation_model,
                                            "raw": raw.replace(owner["secret"], "[redacted]"),
                                            "quality_history": json.loads(json.dumps(quality_history, ensure_ascii=False).replace(owner["secret"], "[redacted]")),
                                        })
                                        can_retry = quality_failures <= repair_limit
                                        update(chunk_id, "quality_failed",
                                            f"{chunk_id} chưa đạt: {quality_errors[0][:200]}. "
                                            + (f"Tự chạy lại ngay ({quality_failures}/{repair_limit})." if can_retry else
                                               "Đã hết lượt tự sửa; giữ checkpoint để tiếp tục."),
                                            quality_errors=quality_errors[:20], repair_attempt=quality_failures)
                                        if not can_retry:
                                            raise QualityRejected(f"{chunk_id} chưa khớp video sau các lượt tự sửa: {quality_errors[0][:250]}")
                                        continue
                                    update(chunk_id, "gemini_checking", f"{chunk_id} đã qua đối chiếu Gemini",
                                           quality_errors=[], repair_attempt=quality_failures)
                                    result = {"version": CONTENT_SCHEMA_VERSION, "cache_id": cache_id, "chunk_id": chunk_id,
                                        "raw": raw, "cues": cues, "model": generation_model, "key_id": owner["id"],
                                        "key_name": owner["name"], "cached": False, "warnings": cleanup_warnings,
                                        "usage": usage_totals, "quality": quality, "quality_attempts": len(quality_history),
                                        "upload_bytes": proxy.stat().st_size}
                                    if not cues and any(start < chunk["core_end_ms"] and end > chunk["core_start_ms"] for start, end in manifest["speech"]):
                                        result["warnings"].append({"code": "gemini_possible_missing_speech", "message": "Đoạn có tín hiệu tiếng nói nhưng Gemini không trả cue; cần xem lại.",
                                                                   "start_ms": chunk["core_start_ms"], "end_ms": chunk["core_end_ms"]})
                                    return result
                            except GeminiApiError as exc:
                                service.dispatcher.report(owner, execution["model"], exc.failure)
                                if attempt >= settings.max_retries or exc.failure.category not in {"quota", "authentication", "permission"}:
                                    raise
                                update(chunk_id, "quota_wait" if exc.failure.category == "quota" else "retrying", str(exc))
                            except (ValueError, TypeError) as exc:
                                previous_raw = raw
                                feedback = "Invalid generation or audit response: " + str(exc)[:2000]
                                if isinstance(raw, str) and len(raw.encode("utf-8")) <= 8_000_000:
                                    redacted = raw.replace(owner["secret"], "[redacted]")
                                    atomic_json(checkpoint_dir / "rejections" / f"{chunk_id}.json", {
                                        "version": 1, "chunk_id": chunk_id, "model": execution["model"],
                                        "error_type": type(exc).__name__, "raw": redacted})
                                if attempt >= min(2, settings.max_retries):
                                    raise
                                update(chunk_id, "retrying", f"Kết quả {chunk_id} chưa hợp lệ; kiểm tra lại trong ngân sách retry")
                            finally:
                                if file_name:
                                    try:
                                        # Cleanup remains bound to the snapshot that created this upload,
                                        # even when the key was deleted or the job canceled meanwhile.
                                        cleaned = service._delete_file(file_name, client=client, api_key=owner["secret"], cancel_event=threading.Event())
                                    except Exception:
                                        cleaned = False
                                    if not cleaned:
                                        cleanup_warnings.append({"code": "gemini_remote_cleanup_failed", "message": "Không thể xóa tệp tạm trên Gemini; hãy kiểm tra lại sau.", "key_id": owner["id"], "file_name": file_name})
                raise GeminiSubtitleError("Đã hết ngân sách thử lại đoạn Gemini.")
            finally:
                proxy.unlink(missing_ok=True)

        # Keep the same job running while recovering missing chunks. A completed
        # chunk is never resubmitted; fresh request budgets apply only to recovery.
        recovery_limit = min(AUTO_RECOVERY_ROUNDS, max(0, settings.max_retries))
        remaining_chunks = chunks
        recovery_round = 0
        def fill(executor, pending, iterator, halted):
            while len(pending) < workers and not halted and not context.cancel_event.is_set():
                chunk = next(iterator, None)
                if chunk is None:
                    break
                pending[executor.submit(process_chunk, chunk)] = chunk
        for recovery_round in range(recovery_limit + 1):
            context.raise_if_canceled()
            if recovery_round:
                delay = min(60, max(5, settings.retry_base_seconds) * 2 ** (recovery_round - 1))
                def report_recovery(seconds, recovery_chunks=tuple(remaining_chunks), current_round=recovery_round):
                    for chunk in recovery_chunks:
                        update(chunk["chunk_id"], "retry_wait",
                               f"Đã xong {len(completed)}/{len(chunks)} đoạn · Tự bổ sung {len(recovery_chunks)} đoạn còn thiếu "
                               f"sau {seconds}s (lượt {current_round}/{recovery_limit})", recovery_round=current_round)
                _wait_for_recovery(delay, context, report_recovery)
                context.raise_if_canceled()
                # Workers from the previous round have all stopped before resetting.
                fallback.reset_temporary()
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gemini-chunk") as executor:
                pending = {}
                halted = False
                halted_for_recovery = False
                iterator = iter(remaining_chunks)
                fill(executor, pending, iterator, halted)
                while pending:
                    done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in done:
                        chunk = pending.pop(future)
                        chunk_id = chunk["chunk_id"]
                        try:
                            result = future.result()
                            if not context.cancel_event.is_set():
                                atomic_json(checkpoint_dir / f"{chunk_id}.json", {k: v for k, v in result.items() if k != "cues"})
                                with progress_lock:
                                    completed[chunk_id] = result
                                failures.pop(chunk_id, None)
                                failure_errors.pop(chunk_id, None)
                                update(chunk_id, "completed", f"Đã hoàn tất {len(completed)}/{len(chunks)} đoạn", model=result["model"], key_id=result["key_id"], key_name=result["key_name"])
                        except Exception as exc:
                            if isinstance(exc, NoEligibleKeys):
                                halted = True
                                halted_for_recovery |= exc.retryable
                            failures[chunk_id] = str(exc)
                            failure_errors[chunk_id] = exc
                            if not context.cancel_event.is_set():
                                update(chunk_id, "failed", f"{chunk_id}: {str(exc)[:300]}")
                    if context.cancel_event.is_set():
                        for future in pending:
                            future.cancel()
                    else:
                        fill(executor, pending, iterator, halted)
            context.raise_if_canceled()
            remaining_chunks = [chunk for chunk in chunks if chunk["chunk_id"] not in completed and (
                _recoverable_chunk_error(failure_errors[chunk["chunk_id"]])
                if chunk["chunk_id"] in failure_errors else halted_for_recovery)]
            if not remaining_chunks:
                break
        context.raise_if_canceled()
        if failures or len(completed) != len(chunks):
            atomic_json(checkpoint_dir / "failure.json", {"version": 1, "chunks": states, "failures": failures})
            recovery_note = f"Đã tự thử lại {recovery_round} lượt. " if recovery_round else ""
            raise GeminiSubtitleError(f"Chưa hoàn tất {len(chunks) - len(completed)}/{len(chunks)} đoạn. " + recovery_note + "Đã giữ checkpoint để tiếp tục. " + next(iter(failures.values()), ""))
        ordered = [completed[chunk["chunk_id"]] for chunk in chunks]
        context.update(93, "checking_boundaries", "Đang đối chiếu vùng nối phụ đề")
        cues, warnings, audit = merge_chunks(ordered, manifest)
        for result in ordered:
            warnings.extend(result["warnings"])
        document = SubtitleDocumentV2(language="vi", segments=cues, run_id=options.get("run_id") or context.job_id,
            timing_precision_ms=max([100] + [cue.get("timing_precision_ms", 100) for cue in cues])).model_dump(mode="json")
        if options.get("alignment_mode", "off") != "off":
            from ..config import settings as app_settings
            from .subtitle_alignment import AlignmentSettings, align_subtitle_document
            selected_ids = {cue["id"] for cue in cues if cue.get("needs_review")} if options["alignment_mode"] == "review" else None
            context.update(95, "aligning_audio", "Đang căn lời gốc với audio theo phạm vi đã chọn")
            try:
                alignment = align_subtitle_document(video, document, media,
                    settings=AlignmentSettings(engine=options.get("alignment_engine", "faster_whisper"),
                        whisper_model=app_settings.content_bot_alignment_whisper_model,
                        whisper_device=app_settings.content_bot_alignment_whisper_device,
                        whisper_compute_type=app_settings.content_bot_alignment_whisper_compute_type,
                        whisper_model_dir=app_settings.data_dir / "models" / "faster-whisper",
                        whisper_allow_download=app_settings.content_bot_alignment_whisper_allow_download,
                        cpu_threads=app_settings.content_bot_alignment_cpu_threads,
                        preserve_display=True),
                    cue_ids=selected_ids, cancel_event=context.cancel_event,
                    cache_dir=app_settings.data_dir / "cache" / "subtitle-alignment")
                document = SubtitleDocumentV2.model_validate(alignment["document"]).model_dump(mode="json")
                cues = document["segments"]
                warnings.extend(alignment["warnings"])
            except (RuntimeError, ValueError, OSError, ImportError) as exc:
                context.raise_if_canceled()
                warnings.append({"code": "optional_alignment_unavailable", "message": "Không thể căn audio bằng engine/model hiện có; giữ phụ đề Gemini để duyệt.", "error_type": type(exc).__name__})
        warnings.extend(validate_cues(document["segments"], media_duration_ms=media["duration_ms"]))
        return {"document": document, "warnings": warnings, "srt": subtitles_to_srt(cues),
            "segment_count": len(cues), "processing_seconds": round(time.monotonic() - started, 3),
            "provider": "gemini_api", "model": model, "actual_models": sorted({r["model"] for r in ordered}),
            "chunk_count": len(chunks), "chunks": list(states.values()), "manifest": manifest,
            "quality_checks": [{"chunk_id": r["chunk_id"], "method": r["quality"]["method"],
                                "model": r["quality"]["model"], "attempts": r.get("quality_attempts", 1),
                                "timing_tolerance_ms": r["quality"]["timing_tolerance_ms"]} for r in ordered],
            "boundary_audit": audit, "usage": [{"chunk_id": r["chunk_id"], "model": r["model"], "cached": r["cached"], **r["usage"]} for r in ordered if r.get("usage")], "pipeline_version": 1}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
