from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from .gemini_dispatch import (
    ApiFailure,
    GeminiDispatcher,
    ModelFallback,
    classify_failure,
)

logger = logging.getLogger("content_bot.gemini")

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
_DEPRECATED_GEMINI_MODELS = frozenset({"gemini-2.0-flash"})
_GEMINI_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_deprecated_model_warning_emitted = False


class GeminiSubtitleError(RuntimeError):
    pass


class GeminiSubtitleCanceled(GeminiSubtitleError):
    pass


class GeminiApiError(GeminiSubtitleError):
    def __init__(self, failure: ApiFailure):
        self.failure = failure
        labels = {"quota": "Giới hạn tốc độ/quota", "authentication": "API key không hợp lệ",
                  "permission": "Không có quyền truy cập key/project/model", "overloaded": "Model quá tải",
                  "model_unavailable": "Model không tồn tại hoặc không hỗ trợ", "transient": "Dịch vụ tạm không sẵn sàng",
                  "request": "Yêu cầu Gemini không hợp lệ"}
        super().__init__(f"Gemini: {labels[failure.category]} (HTTP {failure.status}) [{failure.operation}]")


@dataclass(frozen=True)
class GeminiSubtitleSettings:
    job_root: Path
    api_key: str | None = None
    model: str = DEFAULT_GEMINI_MODEL
    timeout_seconds: int = 1800
    chunk_seconds: int = 120
    max_input_mb: int = 19
    max_retries: int = 5
    retry_base_seconds: float = 3.0
    max_concurrent: int = 0  # Legacy field, ignored: one slot per enabled key.
    group_concurrent: int = 0  # Legacy field, ignored.
    compression_concurrent: int = 2
    max_chunk_seconds: int = 600
    context_seconds: float = 2.0
    min_pause_ms: int = 800
    checkpoint_retention_days: int = 7
    checkpoint_max_mb: int = 2048


PROMPT_VERSION = 13

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com"


def gemini_generation_cache_key(
    media: dict[str, Any],
    options: dict[str, Any],
    *,
    model: str,
) -> str:
    payload = {
        "audio_hash": media.get("audio_hash"),
        "fingerprint": media.get("fingerprint"),
        "model": model,
        "options": options,
        "prompt_version": PROMPT_VERSION,
        "quality_version": _quality_version(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _quality_version() -> int:
    from .gemini_quality import QUALITY_VERSION
    return QUALITY_VERSION


def _interruptible_sleep(seconds: float, cancel_event: threading.Event) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel_event.is_set():
            raise GeminiSubtitleCanceled("Da huy tao phu de bang Gemini")
        time.sleep(min(1.0, deadline - time.monotonic()))


class GeminiSubtitleService:
    def __init__(
        self,
        settings: GeminiSubtitleSettings,
        api_key_provider: Callable[[], str | None] | None = None,
        keyring_provider: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.settings = settings
        self._api_key_provider = api_key_provider
        self._keyring_provider = keyring_provider
        self._request_context = threading.local()
        self.dispatcher = GeminiDispatcher(self.runtime_keys, max_concurrent=settings.max_concurrent,
                                           group_concurrent=settings.group_concurrent,
                                           compression_concurrent=settings.compression_concurrent)

    def runtime_keys(self) -> list[dict[str, Any]]:
        if self._keyring_provider is not None:
            return self._keyring_provider()
        value = self._api_key()
        return [{"id": "gemini-legacy", "name": "Gemini", "enabled": True,
                 "project_group": None, "secret": value}] if value else []

    def _guard_model(self, owner: dict[str, Any], model: str) -> None:
        failure = self.dispatcher.blocked_failure(owner, model)
        if failure is not None:
            raise GeminiApiError(failure)

    def cache_policy(self) -> dict[str, Any]:
        from .gemini_dispatch import FLASH_FALLBACK
        from .gemini_media import PLANNER_VERSION, VAD_SHA256
        return {"target_seconds": self.settings.chunk_seconds, "max_chunk_seconds": self.settings.max_chunk_seconds,
                "min_pause_ms": self.settings.min_pause_ms, "context_seconds": self.settings.context_seconds,
                "max_input_mb": self.settings.max_input_mb, "planner": PLANNER_VERSION,
                "vad": VAD_SHA256, "fallback": list(FLASH_FALLBACK), "pipeline": 1, "prompt": PROMPT_VERSION,
                "response_schema": 1, "quality_version": _quality_version()}

    def _api_key(self) -> str | None:
        value = self._api_key_provider() if self._api_key_provider else self.settings.api_key
        if self._api_key_provider is None:
            value = value or os.environ.get("GEMINI_API_KEY")
        return value.strip() if value and value.strip() else None

    @staticmethod
    def normalize_model(model: str | None) -> str:
        candidate = (model or DEFAULT_GEMINI_MODEL).strip()
        if candidate.startswith("models/"):
            candidate = candidate.removeprefix("models/")
        if not _GEMINI_MODEL_PATTERN.fullmatch(candidate):
            raise GeminiSubtitleError("Gemini model không hợp lệ.")
        if candidate in _DEPRECATED_GEMINI_MODELS:
            global _deprecated_model_warning_emitted
            if not _deprecated_model_warning_emitted:
                logger.warning(
                    "Gemini model %s is no longer available; using %s",
                    candidate,
                    DEFAULT_GEMINI_MODEL,
                )
                _deprecated_model_warning_emitted = True
            return DEFAULT_GEMINI_MODEL
        return candidate

    def resolve_model(self, requested_model: str | None = None) -> str:
        return self.normalize_model(requested_model or self.settings.model)

    def status(self) -> dict[str, Any]:
        key = self._api_key()
        if not key:
            return {
                "installed": True,
                "authenticated": False,
                "auth_method": None,
                "issue": "no_api_key",
                "provider": "api_direct",
            }
        return {
            "installed": True,
            "authenticated": True,
            "auth_method": "gemini_api_key",
            "issue": None,
            "provider": "api_direct",
            "model": self.resolve_model(),
        }

    def list_models(self) -> list[dict[str, Any]]:
        api_key = self._api_key()
        if not api_key:
            raise GeminiSubtitleError("Chưa cấu hình Gemini API key.")

        models: list[dict[str, Any]] = []
        page_token: str | None = None
        with self._client() as client:
            while True:
                params: dict[str, str] = {"pageSize": "1000"}
                if page_token:
                    params["pageToken"] = page_token
                response = client.get(
                    "/v1beta/models",
                    headers=self._api_headers(api_key),
                    params=params,
                    timeout=30,
                )
                if response.status_code != 200:
                    self._raise_for_gemini(response, "models.list")
                payload = response.json()
                for item in payload.get("models", []):
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "")
                    model_id = str(item.get("baseModelId") or name.removeprefix("models/"))
                    methods = item.get("supportedGenerationMethods") or []
                    if "generateContent" not in methods:
                        continue
                    if not model_id.startswith("gemini-"):
                        continue
                    if not _GEMINI_MODEL_PATTERN.fullmatch(model_id):
                        continue
                    models.append(
                        {
                            "id": model_id,
                            "display_name": str(item.get("displayName") or model_id),
                            "description": str(item.get("description") or ""),
                            "input_token_limit": item.get("inputTokenLimit"),
                            "output_token_limit": item.get("outputTokenLimit"),
                        }
                    )
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break

        unique = {item["id"]: item for item in models}
        selected = self.resolve_model()
        return sorted(
            unique.values(),
            key=lambda item: (item["id"] != selected, item["id"]),
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=_GEMINI_API_BASE,
            timeout=httpx.Timeout(connect=30, read=600, write=600, pool=10),
        )

    @staticmethod
    def _api_headers(api_key: str | None) -> dict[str, str]:
        return {"x-goog-api-key": api_key} if api_key else {}

    @staticmethod
    def _response_error_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
            detail = (
                body.get("error", {}).get("message")
                or body.get("message")
                or response.text
            )
        except Exception:
            detail = response.text
        detail = " ".join(str(detail or "").split())
        try:
            secret = response.request.headers.get("x-goog-api-key")
            if secret:
                detail = detail.replace(secret, "[redacted]")
        except RuntimeError:
            pass
        detail = re.sub(r"AIza[A-Za-z0-9_-]{20,}", "[redacted]", detail)
        return detail[:240]

    @classmethod
    def _http_retry_reason(cls, response: httpx.Response, operation: str) -> str:
        if response.status_code == 429:
            reason = f"Gemini giới hạn tốc độ/quota (HTTP 429) khi {operation}"
        else:
            reason = (
                "Dịch vụ Gemini tạm không sẵn sàng "
                f"(HTTP {response.status_code}) khi {operation}"
            )
        detail = cls._response_error_detail(response)
        return f"{reason}: {detail}" if detail else reason

    def _retry_delay(self, _attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            if response.status_code == 429:
                return max(self.settings.retry_base_seconds, classify_failure(response, "retry").wait_seconds)
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after.isdigit():
                return max(float(retry_after), self.settings.retry_base_seconds)
        # Keep retries responsive for interactive subtitle jobs. If Gemini
        # explicitly asks us to wait longer via Retry-After, that value still
        # takes precedence above.
        return min(self.settings.retry_base_seconds, 120.0)

    def _wait_for_retry(
        self,
        attempt: int,
        cancel_event: threading.Event,
        *,
        reason: str,
        response: httpx.Response | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        wait = self._retry_delay(attempt, response)
        deadline = getattr(self._request_context, "deadline", None)
        if deadline is not None and time.monotonic() + wait >= deadline:
            raise GeminiSubtitleError("Thời gian chờ server vượt deadline xử lý đoạn; đã giữ checkpoint.")
        if status_callback:
            status_callback(
                f"{reason}; thử lại sau {int(wait)}s "
                f"({attempt}/{self.settings.max_retries})"
            )
        _interruptible_sleep(wait, cancel_event)

    def _raise_for_gemini(self, resp: httpx.Response, context: str = "") -> None:
        raise GeminiApiError(classify_failure(resp, context))

    def _create_proxy(
        self,
        video_path: Path,
        output_path: Path,
        *,
        start_seconds: float,
        duration_seconds: float,
        cancel_event: threading.Event,
        deadline: float | None = None,
    ) -> None:
        target_kilobits = max(1024, self.settings.max_input_mb * 8 * 1024 - 1024)
        total_kbps = target_kilobits / max(1.0, duration_seconds)
        audio_kbps = 64
        video_kbps = max(180, min(1800, round(total_kbps - audio_kbps)))
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-copyts", "-start_at_zero",
            "-ss", f"{start_seconds:.3f}",
            "-i", str(video_path.resolve()),
            "-t", f"{duration_seconds:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"setpts=PTS-{start_seconds:.3f}/TB,scale='min(854,iw)':-2,fps=24:start_time=0",
            "-af", f"asetpts=PTS-{start_seconds:.3f}/TB,aresample=async=1:first_pts=0",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            "-y", str(output_path),
        ]
        deadline = min(deadline or float("inf"), time.monotonic() + max(300, round(duration_seconds * 3)))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            while True:
                if cancel_event.is_set():
                    raise GeminiSubtitleCanceled("Da huy tao phu de bang Gemini")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GeminiSubtitleError("FFmpeg xu ly proxy qua thoi gian")
                try:
                    proc.communicate(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc.communicate()
        if proc.returncode != 0 or not output_path.is_file():
            raise GeminiSubtitleError("FFmpeg loi khi tao proxy video.")
        limit_bytes = self.settings.max_input_mb * 1024 * 1024
        if output_path.stat().st_size > limit_bytes:
            raise GeminiSubtitleError(
                f"Khong nen duoc video proxy duoi gioi han {self.settings.max_input_mb} MB; "
                "hay giam do dai video hoac tang so doan chia."
            )

    def _upload_file(
        self,
        proxy_path: Path,
        *,
        cancel_event: threading.Event,
        status_callback: Callable[[str], None] | None = None,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        managed: bool = False,
    ) -> str:
        size_bytes = proxy_path.stat().st_size
        display_name = proxy_path.name
        owned_client = client is None
        client = client or self._client()
        api_key = api_key or self._api_key()
        file_name = None
        activated = False
        try:
            if cancel_event.is_set():
                raise GeminiSubtitleCanceled("Da huy tao phu de bang Gemini")
            init_headers = {
                **self._api_headers(api_key or self._api_key()),
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size_bytes),
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
                "Content-Type": "application/json",
            }
            init_resp: httpx.Response | None = None
            for attempt in range(self.settings.max_retries + 1):
                try:
                    init_resp = client.post(
                        "/upload/v1beta/files",
                        params={"uploadType": "resumable"},
                        headers=init_headers,
                        content=json.dumps({"file": {"display_name": display_name}}).encode(),
                    )
                except httpx.TimeoutException as exc:
                    if attempt >= self.settings.max_retries:
                        raise GeminiSubtitleError(
                            "Hết thời gian chờ Gemini File API khi khởi tạo upload."
                        ) from exc
                    self._wait_for_retry(
                        attempt + 1,
                        cancel_event,
                        reason="Hết thời gian chờ Gemini File API khi khởi tạo upload",
                        status_callback=status_callback,
                    )
                    continue
                except httpx.TransportError as exc:
                    if attempt >= self.settings.max_retries:
                        raise GeminiSubtitleError(
                            "Mất kết nối tới Gemini File API khi khởi tạo upload."
                        ) from exc
                    self._wait_for_retry(
                        attempt + 1,
                        cancel_event,
                        reason="Mất kết nối tới Gemini File API khi khởi tạo upload",
                        status_callback=status_callback,
                    )
                    continue
                if managed and init_resp.status_code == 429:
                    self._raise_for_gemini(init_resp, "initiate upload")
                if init_resp.status_code in (429,) or 500 <= init_resp.status_code < 600:
                    if attempt >= self.settings.max_retries:
                        break
                    self._wait_for_retry(
                        attempt + 1,
                        cancel_event,
                        reason=self._http_retry_reason(init_resp, "khởi tạo upload"),
                        response=init_resp,
                        status_callback=status_callback,
                    )
                    continue
                break
            assert init_resp is not None
            if init_resp.status_code not in (200, 308):
                self._raise_for_gemini(init_resp, "initiate upload")
            upload_url = init_resp.headers.get("X-Goog-Upload-URL", "")
            if not upload_url:
                raise GeminiSubtitleError(
                    "Gemini File API khong tra ve upload URL. Kiem tra API key va quota."
                )
            if cancel_event.is_set():
                raise GeminiSubtitleCanceled("Da huy tao phu de bang Gemini")
            with proxy_path.open("rb") as video_file:
                upload_resp = client.post(
                    upload_url,
                    headers={
                        **self._api_headers(api_key or self._api_key()),
                        "Content-Length": str(size_bytes),
                        "X-Goog-Upload-Offset": "0",
                        "X-Goog-Upload-Command": "upload, finalize",
                    },
                    content=video_file,
                )
            if upload_resp.status_code not in (200, 201):
                self._raise_for_gemini(upload_resp, "upload bytes")
            file_info = upload_resp.json()
            file_name = file_info.get("file", {}).get("name", "")
            if not file_name:
                raise GeminiSubtitleError(
                    "Gemini File API khong tra ve ten file sau khi upload."
                )
            deadline = time.monotonic() + 120
            poll_attempt = 0
            while True:
                if cancel_event.is_set():
                    raise GeminiSubtitleCanceled("Da huy tao phu de bang Gemini")
                if time.monotonic() > deadline:
                    raise GeminiSubtitleError(
                        "Gemini File API xu ly file qua 120 giay."
                    )
                try:
                    state_resp = client.get(
                        f"/v1beta/{file_name}",
                        headers=self._api_headers(api_key or self._api_key()),
                    )
                except httpx.TimeoutException as exc:
                    if poll_attempt >= self.settings.max_retries:
                        raise GeminiSubtitleError(
                            "Hết thời gian chờ Gemini File API khi kiểm tra file."
                        ) from exc
                    poll_attempt += 1
                    self._wait_for_retry(
                        poll_attempt,
                        cancel_event,
                        reason="Hết thời gian chờ Gemini File API khi kiểm tra file",
                        status_callback=status_callback,
                    )
                    continue
                except httpx.TransportError as exc:
                    if poll_attempt >= self.settings.max_retries:
                        raise GeminiSubtitleError(
                            "Mất kết nối tới Gemini File API khi kiểm tra file."
                        ) from exc
                    poll_attempt += 1
                    self._wait_for_retry(
                        poll_attempt,
                        cancel_event,
                        reason="Mất kết nối tới Gemini File API khi kiểm tra file",
                        status_callback=status_callback,
                    )
                    continue
                if managed and state_resp.status_code == 429:
                    self._raise_for_gemini(state_resp, "poll file state")
                if state_resp.status_code == 429 or 500 <= state_resp.status_code < 600:
                    if poll_attempt >= self.settings.max_retries:
                        self._raise_for_gemini(state_resp, "poll file state")
                    poll_attempt += 1
                    self._wait_for_retry(
                        poll_attempt,
                        cancel_event,
                        reason=self._http_retry_reason(state_resp, "kiểm tra file"),
                        response=state_resp,
                        status_callback=status_callback,
                    )
                    continue
                poll_attempt = 0
                if state_resp.status_code != 200:
                    self._raise_for_gemini(state_resp, "poll file state")
                state_info = state_resp.json()
                state = state_info.get("state", "")
                if state == "ACTIVE":
                    activated = True
                    return file_name
                if state == "FAILED":
                    raise GeminiSubtitleError(
                        "Gemini File API bao file FAILED sau khi upload."
                    )
                _interruptible_sleep(2, cancel_event)
        finally:
            if file_name and not activated:
                try:
                    cleaned = self._delete_file(file_name, client=client, api_key=api_key, cancel_event=threading.Event())
                except Exception:
                    cleaned = False
                if not cleaned:
                    logger.warning("Gemini incomplete upload cleanup failed for %s", file_name)
            if owned_client:
                client.close()

    def _delete_file(
        self,
        file_name: str,
        *,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        cancel_event = cancel_event or threading.Event()
        owned_client = client is None
        client = client or self._client()
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    response = client.delete(
                        f"/v1beta/{file_name}",
                        headers=self._api_headers(api_key or self._api_key()),
                        timeout=10,
                    )
                except httpx.TransportError as exc:
                    if attempt >= min(self.settings.max_retries, 2):
                        logger.warning("Gemini remote cleanup failed: %s", type(exc).__name__)
                        return False
                    self._wait_for_retry(
                        attempt + 1,
                        cancel_event,
                        reason="Mất kết nối khi xóa file tạm trên Gemini",
                    )
                    continue
                if response.status_code in (200, 204, 404):
                    return True
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if self._retry_delay(attempt, response) > 10:
                        return False
                    if attempt >= min(self.settings.max_retries, 2):
                        logger.warning("Gemini remote cleanup failed with HTTP %s", response.status_code)
                        return False
                    self._wait_for_retry(
                        attempt + 1,
                        cancel_event,
                        reason=self._http_retry_reason(response, "xóa file tạm"),
                        response=response,
                    )
                    continue
                logger.warning("Gemini remote cleanup failed with HTTP %s", response.status_code)
                return False
            return False
        finally:
            if owned_client:
                client.close()

    def _generate_content(
        self,
        file_name: str,
        prompt_text: str,
        *,
        cancel_event: threading.Event,
        status_callback: Any = None,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        model: str | None = None,
        fallback: ModelFallback | None = None,
        quota_scope: str = "unknown-project",
        managed: bool = False,
        execution: dict[str, Any] | None = None,
        chunk_deadline: float | None = None,
        response_schema: dict[str, Any] | None = None,
        model_guard: Callable[[str], None] | None = None,
        read_timeout_seconds: float = 600,
    ) -> str:
        selected_model = self.resolve_model(model)
        fallback = fallback or ModelFallback(selected_model)
        selected_model = fallback.next_model(quota_scope)
        file_uri = f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}},
                        {"text": prompt_text},
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 65536,
            },
        }
        if response_schema is not None:
            from .gemini_schema import response_schema as wire_schema
            payload["generationConfig"]["responseSchema"] = wire_schema(response_schema)
        deadline = min(chunk_deadline or float("inf"), time.monotonic() + self.settings.timeout_seconds)
        attempt = 0
        max_retries = self.settings.max_retries
        owned_client = client is None
        client = client or self._client()
        try:
            while True:
                if cancel_event.is_set():
                    raise GeminiSubtitleCanceled("Da huy tao phu de bang Gemini")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GeminiSubtitleError(
                        "Gemini generateContent qua thoi gian cho phep."
                    )
                selected_model = fallback.next_model(quota_scope)
                url = f"/v1beta/models/{selected_model}:generateContent"
                if execution is not None:
                    execution["model"] = selected_model
                if model_guard is not None:
                    model_guard(selected_model)
                try:
                    resp = client.post(
                        url,
                        headers=self._api_headers(api_key or self._api_key()),
                        json=payload,
                        timeout=httpx.Timeout(
                            connect=30,
                            read=min(read_timeout_seconds, remaining),
                            write=60,
                            pool=10,
                        ),
                    )
                except httpx.TimeoutException as exc:
                    if attempt >= max_retries:
                        raise GeminiSubtitleError(
                            "Hết thời gian chờ Gemini khi phân tích video sau nhiều lần thử."
                        ) from exc
                    attempt += 1
                    self._wait_for_retry(
                        attempt,
                        cancel_event,
                        reason="Hết thời gian chờ Gemini khi phân tích video",
                        status_callback=status_callback,
                    )
                    continue
                except httpx.TransportError as exc:
                    if attempt >= max_retries:
                        raise GeminiSubtitleError(
                            "Mất kết nối tới Gemini khi phân tích video sau nhiều lần thử."
                        ) from exc
                    attempt += 1
                    self._wait_for_retry(
                        attempt,
                        cancel_event,
                        reason="Mất kết nối tới Gemini khi phân tích video",
                        status_callback=status_callback,
                    )
                    continue
                if resp.status_code not in (200, 201):
                    failure = classify_failure(resp, "generateContent")
                    if failure.category in {"overloaded", "model_unavailable"}:
                        fallback.block(quota_scope, selected_model, temporary=failure.category == "overloaded")
                        try:
                            next_model = fallback.next_model(quota_scope)
                        except RuntimeError as exc:
                            raise GeminiApiError(failure) from exc
                        if attempt >= max_retries:
                            raise GeminiApiError(failure)
                        attempt += 1
                        if status_callback:
                            status_callback(f"{selected_model} không sẵn sàng (HTTP {resp.status_code}); chuyển ngay sang {next_model}")
                        continue
                    if managed and failure.category in {"quota", "authentication", "permission"}:
                        raise GeminiApiError(failure)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt >= max_retries:
                        self._raise_for_gemini(resp, "generateContent")
                    attempt += 1
                    self._wait_for_retry(
                        attempt,
                        cancel_event,
                        reason=self._http_retry_reason(resp, "phân tích video"),
                        response=resp,
                        status_callback=status_callback,
                    )
                    continue
                if resp.status_code not in (200, 201):
                    self._raise_for_gemini(resp, "generateContent")
                data = resp.json()
                blocked = data.get("promptFeedback", {}).get("blockReason")
                finish_reason = (data.get("candidates") or [{}])[0].get("finishReason")
                if blocked or finish_reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
                    raise GeminiSubtitleError("Gemini từ chối nội dung theo cơ chế bảo vệ; tác vụ không đổi key/model để vượt chặn.")
                try:
                    parts = data["candidates"][0]["content"]["parts"]
                    text = "".join(part["text"] for part in parts if not part.get("thought") and isinstance(part.get("text"), str))
                    if not text.strip():
                        raise ValueError("No output text parts")
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    finish = (
                        data.get("candidates", [{}])[0].get("finishReason", "UNKNOWN")
                        if data.get("candidates")
                        else "NO_CANDIDATES"
                    )
                    raise GeminiSubtitleError(
                        f"Gemini khong tra ve noi dung phu de (finishReason={finish}). "
                        "Kiểm tra kết quả và trạng thái tác vụ."
                    ) from exc
                if execution is not None:
                    execution["model"] = selected_model
                    execution["usage"] = {key: value for key, value in (data.get("usageMetadata") or {}).items()
                                          if key in {"promptTokenCount", "candidatesTokenCount", "totalTokenCount", "thoughtsTokenCount", "cachedContentTokenCount"}
                                          and isinstance(value, int) and not isinstance(value, bool)}
                return text.strip()
        finally:
            if owned_client:
                client.close()

    def _audit_content(self, file_name: str, prompt: str, **kwargs) -> str:
        """A separate media observation request, with the same dispatch/budget rules."""
        return self._generate_content(file_name, prompt, **kwargs)

    @staticmethod
    def _build_prompt(
        *,
        bilingual: bool,
        chunk_index: int,
        chunk_count: int,
        chunk_duration_ms: int,
    ) -> str:
        from .gemini_prompts import build_generation_prompt
        return build_generation_prompt(
            bilingual=bilingual, chunk_index=chunk_index, chunk_count=chunk_count,
            chunk_duration_ms=chunk_duration_ms,
        )

    @staticmethod
    def _offset_cues(
        cues: list[dict[str, Any]],
        *,
        offset_ms: int,
        chunk_duration_ms: int,
        duration_ms: int,
        existing_count: int,
    ) -> list[dict[str, Any]]:
        shifted: list[dict[str, Any]] = []
        for cue in cues:
            local_start_ms = max(0, int(cue["start_ms"]))
            local_end_ms = min(chunk_duration_ms, int(cue["end_ms"]))
            if local_start_ms >= chunk_duration_ms or local_end_ms <= local_start_ms:
                continue
            start_ms = local_start_ms + offset_ms
            end_ms = min(duration_ms, local_end_ms + offset_ms)
            if end_ms <= start_ms:
                continue
            next_cue = {
                **cue,
                "id": f"gm-{existing_count + len(shifted) + 1:04d}-{start_ms}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "timing_source": "gemini_estimate",
                "timing_precision_ms": max(100, int(cue.get("timing_precision_ms", 100))),
            }
            shifted.append(next_cue)
        return shifted

    def generate(self, video_path: Path, media: dict[str, Any], options: dict[str, Any], context: Any) -> dict[str, Any]:
        from .gemini_pipeline import generate_pipeline
        return generate_pipeline(self, video_path, media, options, context)
