from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from ..schemas import SubtitleDocumentV2
from .subtitle_timing import validate_cues
from .subtitles import parse_subtitles_v2, subtitles_to_srt

logger = logging.getLogger("content_bot.gemini")

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
_DEPRECATED_GEMINI_MODELS = frozenset({"gemini-2.0-flash"})
_GEMINI_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_deprecated_model_warning_emitted = False


class GeminiSubtitleError(RuntimeError):
    pass


class GeminiSubtitleCanceled(GeminiSubtitleError):
    pass


@dataclass(frozen=True)
class GeminiSubtitleSettings:
    job_root: Path
    api_key: str | None = None
    model: str = DEFAULT_GEMINI_MODEL
    timeout_seconds: int = 1800
    chunk_seconds: int = 180
    max_input_mb: int = 19
    max_retries: int = 5
    retry_base_seconds: float = 30.0


PROMPT_VERSION = 6

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
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
    ) -> None:
        self.settings = settings
        self._api_key_provider = api_key_provider

    def _api_key(self) -> str | None:
        value = self._api_key_provider() if self._api_key_provider else self.settings.api_key
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
        return " ".join(str(detail or "").split())[:240]

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

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after.isdigit():
                return min(float(retry_after), 120.0)
        return min(self.settings.retry_base_seconds * (2 ** max(0, attempt - 1)), 120.0)

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
        if status_callback:
            status_callback(
                f"{reason}; thử lại sau {int(wait)}s "
                f"({attempt}/{self.settings.max_retries})"
            )
        _interruptible_sleep(wait, cancel_event)

    def _raise_for_gemini(self, resp: httpx.Response, context: str = "") -> None:
        detail = self._response_error_detail(resp)
        location = f" [{context}]" if context else ""
        if resp.status_code == 429:
            raise GeminiSubtitleError(
                f"Gemini giới hạn tốc độ/quota (HTTP 429){location}"
                + (f": {detail}" if detail else "")
            )
        if resp.status_code == 401:
            raise GeminiSubtitleError(
                "Gemini từ chối API key (HTTP 401). "
                "Kiểm tra lại key trong Settings -> Gemini AI."
            )
        if resp.status_code == 403:
            raise GeminiSubtitleError(
                "API key không có quyền truy cập Gemini hoặc model đã chọn "
                "(HTTP 403). Kiểm tra project, billing và quyền của key."
            )
        raise GeminiSubtitleError(
            f"Gemini API lỗi HTTP {resp.status_code}{location}"
            + (f": {detail}" if detail else "")
        )

    def _create_proxy(
        self,
        video_path: Path,
        output_path: Path,
        *,
        start_seconds: float,
        duration_seconds: float,
        cancel_event: threading.Event,
    ) -> None:
        target_kilobits = max(1024, self.settings.max_input_mb * 8 * 1024 - 1024)
        total_kbps = target_kilobits / max(1.0, duration_seconds)
        audio_kbps = 64
        video_kbps = max(180, min(1800, round(total_kbps - audio_kbps)))
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_seconds:.3f}",
            "-i", str(video_path.resolve()),
            "-t", f"{duration_seconds:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale='min(854,iw)':-2,fps=24",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            "-y", str(output_path),
        ]
        deadline = time.monotonic() + max(300, round(duration_seconds * 3))
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
                "Khong nen duoc video proxy duoi gioi han 19 MB; "
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
    ) -> str:
        size_bytes = proxy_path.stat().st_size
        display_name = proxy_path.name
        owned_client = client is None
        client = client or self._client()
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
                    return file_name
                if state == "FAILED":
                    raise GeminiSubtitleError(
                        "Gemini File API bao file FAILED sau khi upload."
                    )
                _interruptible_sleep(2, cancel_event)
        finally:
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
    ) -> str:
        selected_model = self.resolve_model(model)
        url = f"/v1beta/models/{selected_model}:generateContent"
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
                "response_mime_type": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 65536,
            },
        }
        deadline = time.monotonic() + self.settings.timeout_seconds
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
                try:
                    resp = client.post(
                        url,
                        headers=self._api_headers(api_key or self._api_key()),
                        json=payload,
                        timeout=httpx.Timeout(
                            connect=30,
                            read=min(600, remaining),
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
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError, TypeError) as exc:
                    finish = (
                        data.get("candidates", [{}])[0].get("finishReason", "UNKNOWN")
                        if data.get("candidates")
                        else "NO_CANDIDATES"
                    )
                    raise GeminiSubtitleError(
                        f"Gemini khong tra ve noi dung phu de (finishReason={finish}). "
                        "Thu model khac hoac kiem tra API key."
                    ) from exc
                return text.strip()
        finally:
            if owned_client:
                client.close()

    @staticmethod
    def _build_prompt(
        *,
        bilingual: bool,
        chunk_index: int,
        chunk_count: int,
        chunk_duration_ms: int,
    ) -> str:
        secondary_rule = (
            "Dat nguyen van ngon ngu nguon trong secondary_text cho tung segment."
            if bilingual
            else "Chi dich sang tieng Viet, khong them truong phu."
        )
        secondary_example = (
            ',\n      "secondary_text": "Nguyen van ngon ngu nguon."'
            if bilingual
            else ""
        )
        chunk_duration_s = f"{chunk_duration_ms / 1000:.3f}".rstrip("0").rstrip(".")
        gap_threshold = chunk_duration_ms // 3000 if chunk_duration_ms > 30_000 else 10
        return (
            f"Ban la bien tap vien phu de tieng Viet chuyen nghiep. Nhiem vu: dich cac phu de/loi thoai THUC SU QUAN SAT DUOC trong video dinh kem. Khong suy dien, khong hoan thanh cau bi cat, khong them canh tiep theo.\n\n"
            f"THONG TIN DOAN: Day la doan {chunk_index}/{chunk_count}. VIDEO_END_MS={chunk_duration_ms}. Thoi luong chinh xac: {chunk_duration_s} giay. Moi timestamp tinh tu dau doan nay (0 ms).\n"
            f"GIOI HAN CUNG: Moi cue phai thoa 0 <= start_ms < end_ms <= {chunk_duration_ms}. Khong co noi dung nao sau VIDEO_END_MS={chunk_duration_ms}.\n\n"
            f"BUOC XU LY BAT BUOC:\n"
            f"1. QUET TIMELINE TU DAU DEN CUOI: Xem video tu 0 ms den dung {chunk_duration_ms} ms. Chi ghi nhan khoang co bang chung truc tiep tu hinh hoac audio trong tep dinh kem.\n"
            f"2. DOC PHU DE TREN HINH: Neu co chu/phu de tren hinh (tieng Trung, Nhat, Han...) - DAY LA NGUON DUY NHAT. Phai dich TUNG cue. Khong gop cue goc. Kho doc dung ... va needs_review=true.\n"
            f"3. KIEM TRA KHOANG TRONG: Ra soat khoang tren {gap_threshold} giay. Chi bo sung cue neu THUC SU nhin thay phu de hoac nghe thay loi thoai trong chinh khoang do; neu khong thi giu nguyen khoang trong.\n"
            f"4. DUNG DUNG LUC: Khi tep ket thuc o {chunk_duration_ms} ms, dung ngay. Neu cau/canh bi cat, khong doan phan con lai va khong dung kien thuc ve phim/video goc de viet tiep.\n\n"
            f"QUY TAC DICH: Uu tien (1) phu de tren hinh -> (2) audio -> (3) ngu canh chi de hieu nghia, KHONG dung ngu canh de tao them loi. Dich tu nhien sang tieng Viet. Khong de sot chu Trung/Nhat/Han da quan sat duoc. Khong bia loi. {secondary_rule}\n\n"
            f"QUY TAC TIMING BAT BUOC:\n"
            f"[T0] GIOI HAN TUYET DOI: 0 <= start_ms < end_ms <= {chunk_duration_ms}. Cue bat dau tai/sau {chunk_duration_ms} ms la khong hop le va phai xoa, khong duoc kep vao cuoi video.\n"
            f"[T1] Moi cue 1-6 giay; max 10s cho loi thoai. Cue > 10s phai tach. Max 84 ky tu.\n"
            f"[T2] end_ms = luc am/phu de ket thuc + max 500ms. KHONG keo den cue tiep theo.\n"
            f"[T3] Im lang/nhac nen/chuyen canh = GAP TRONG (khong tao cue). Ngoai le: loi bai hat, thong bao quan trong.\n"
            f"[T4] start_ms[N+1] >= end_ms[N].\n"
            f"[T5] Khong lam tron: 3.7s -> start_ms=3700.\n"
            f"[T6] Cue theo thu tu thoi gian tang dan. Cue cuoi cung cung phai co end_ms <= {chunk_duration_ms}.\n"
            f"[T7] confidence: 0.95=ro rang, 0.7-0.9=khong chac, <0.7=doan nhieu. needs_review=true khi khong ro loi/timing.\n"
            f"[T8] timing_precision_ms: 100 neu co phu de ro/audio ro; 1000 neu chi chac den giay.\n\n"
            f"UU TIEN BANG CHUNG: So cue co the bang 0 neu doan khong co loi thoai/phu de. Khong tao cue chi de dat mot so luong toi thieu.\n\n"
            f"TU KIEM TRA TRUOC KHI TRA JSON: Tim max(end_ms). Neu gia tri nay > {chunk_duration_ms}, xoa cue nam hoan toan ngoai video va cat end_ms cua cue giao voi diem ket thuc ve dung {chunk_duration_ms}. Sau do kiem tra lai moi cue theo T0.\n\n"
            f"CHI tra ve mot JSON hop le, khong Markdown, khong giai thich:\n"
            '{{\n'
            '  "schema_version": 2,\n'
            '  "language": "vi",\n'
            '  "timebase": "milliseconds",\n'
            '  "timing_source": "gemini_estimate",\n'
            '  "timing_precision_ms": 100,\n'
            '  "gap_warnings": [{{"start_ms": 0, "end_ms": 0, "reason": "example"}}],\n'
            '  "segments": [\n'
            '    {{\n'
            '      "id": "g0001",\n'
            '      "start_ms": 0,\n'
            '      "end_ms": 2500,\n'
            f'      "text": "Phu de tieng Viet."{secondary_example},\n'
            '      "needs_review": false\n'
            '    }}\n'
            '  ]\n'
            '}}'
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

    def generate(
        self,
        video_path: Path,
        media: dict[str, Any],
        options: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        api_key = self._api_key()
        if not api_key:
            raise GeminiSubtitleError(
                "Chua cau hinh Gemini API key. "
                "Vao Settings -> Gemini AI -> nhap API key tu aistudio.google.com/apikey."
            )
        selected_model = self.resolve_model(options.get("model"))
        duration_ms = int(media["duration_ms"])
        workspace = (self.settings.job_root / context.job_id).resolve()
        job_root = self.settings.job_root.resolve()
        if not workspace.is_relative_to(job_root):
            raise GeminiSubtitleError("Thu muc job Gemini khong hop le")
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        chunk_ms = max(30_000, self.settings.chunk_seconds * 1000)
        chunk_count = max(1, math.ceil(duration_ms / chunk_ms))
        # Keep completed responses outside the disposable per-job workspace so a
        # new job can resume after a process restart or a transient API failure.
        source_stat = video_path.stat()
        checkpoint_key = gemini_generation_cache_key(
            {**media, "fingerprint": [media.get("fingerprint"), str(video_path.resolve()),
                                      source_stat.st_size, source_stat.st_mtime_ns]},
            {**options, "chunk_ms": chunk_ms, "duration_ms": duration_ms},
            model=selected_model,
        )
        checkpoint_dir = job_root / "checkpoints" / checkpoint_key
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        combined_cues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        uploaded_files: list[str] = []
        client = self._client()
        try:
            for chunk_index in range(chunk_count):
                context.raise_if_canceled()
                offset_ms = chunk_index * chunk_ms
                current_duration_ms = min(chunk_ms, duration_ms - offset_ms)
                progress = 5 + round((chunk_index / chunk_count) * 85)
                checkpoint_path = checkpoint_dir / f"chunk-{chunk_index + 1:03d}.json"
                raw_text = None
                try:
                    cached_text = checkpoint_path.read_text(encoding="utf-8")
                    cached_document, _ = parse_subtitles_v2(
                        cached_text, media_duration_ms=current_duration_ms
                    )
                    if cached_document["segments"]:
                        raw_text = cached_text
                except (OSError, ValueError, TypeError, KeyError):
                    pass
                if raw_text is None:
                    context.update(
                        progress,
                        "preparing_video",
                        f"Dang nen video ({chunk_index + 1}/{chunk_count})",
                    )
                    chunk_dir = workspace / f"chunk-{chunk_index + 1:03d}"
                    chunk_dir.mkdir(parents=True, exist_ok=True)
                    proxy_path = chunk_dir / "proxy.mp4"
                    can_use_original = (
                        chunk_count == 1
                        and video_path.suffix.lower() == ".mp4"
                        and video_path.stat().st_size <= self.settings.max_input_mb * 1024 * 1024
                    )
                    if can_use_original:
                        shutil.copy2(video_path, proxy_path)
                    else:
                        self._create_proxy(
                            video_path,
                            proxy_path,
                            start_seconds=offset_ms / 1000,
                            duration_seconds=current_duration_ms / 1000,
                            cancel_event=context.cancel_event,
                        )
                    context.update(
                        min(90, progress + 3),
                        "uploading_video",
                        f"Dang tai video len Gemini ({chunk_index + 1}/{chunk_count})",
                    )
                    file_name = self._upload_file(
                        proxy_path,
                        cancel_event=context.cancel_event,
                        status_callback=lambda msg, ci=chunk_index + 1, cc=chunk_count, base_progress=progress: context.update(
                            min(90, base_progress + 3),
                            "uploading_video",
                            f"[{ci}/{cc}] {msg}",
                        ),
                        client=client,
                        api_key=api_key,
                    )
                    uploaded_files.append(file_name)
                    context.update(
                        min(90, progress + 6),
                        "gemini_analyzing",
                        f"Gemini dang xem va dich doan {chunk_index + 1}/{chunk_count}",
                    )
                    prompt = self._build_prompt(
                        bilingual=bool(options.get("bilingual", True)),
                        chunk_index=chunk_index + 1,
                        chunk_count=chunk_count,
                        chunk_duration_ms=current_duration_ms,
                    )
                    raw_text = self._generate_content(
                        file_name,
                        prompt,
                        cancel_event=context.cancel_event,
                        status_callback=lambda msg, ci=chunk_index + 1, cc=chunk_count, base_progress=progress: context.update(
                            min(90, base_progress + 6),
                            "gemini_analyzing",
                            f"[{ci}/{cc}] {msg}",
                        ),
                        client=client,
                        api_key=api_key,
                        model=selected_model,
                    )
                    document, chunk_warnings = parse_subtitles_v2(
                        raw_text, media_duration_ms=current_duration_ms
                    )
                    if not document["segments"]:
                        raise GeminiSubtitleError(
                            f"Gemini khong tra ve cue hop le cho doan {chunk_index + 1}"
                        )
                    temporary = checkpoint_path.with_suffix(f".{context.job_id}.part")
                    temporary.write_text(raw_text, encoding="utf-8")
                    temporary.replace(checkpoint_path)
                else:
                    context.update(progress, "resuming", f"Dùng lại đoạn đã hoàn tất ({chunk_index + 1}/{chunk_count})")
                document, chunk_warnings = parse_subtitles_v2(
                    raw_text, media_duration_ms=current_duration_ms
                )
                shifted = self._offset_cues(
                    document["segments"],
                    offset_ms=offset_ms,
                    chunk_duration_ms=current_duration_ms,
                    duration_ms=duration_ms,
                    existing_count=len(combined_cues),
                )
                combined_cues.extend(shifted)
                warnings.extend(chunk_warnings)
                if len(combined_cues) > 500:
                    raise GeminiSubtitleError("Gemini tra ve qua 500 cue phu de")
            context.update(95, "validating_result", "Dang kiem tra phu de Gemini")
            document_precision = max(
                100,
                *(int(cue.get("timing_precision_ms", 100)) for cue in combined_cues),
            )
            final_doc = SubtitleDocumentV2(
                language="vi",
                timing_source="gemini_estimate",
                timing_precision_ms=document_precision,
                segments=combined_cues,
            ).model_dump(mode="json")
            warnings.extend(
                validate_cues(final_doc["segments"], media_duration_ms=duration_ms)
            )
            return {
                "document": final_doc,
                "warnings": warnings,
                "srt": subtitles_to_srt(final_doc["segments"]),
                "segment_count": len(combined_cues),
                "processing_seconds": round(time.monotonic() - started, 3),
                "provider": "gemini_api",
                "model": selected_model,
                "chunk_count": chunk_count,
                "usage": [],
            }
        finally:
            if workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)
            for fname in uploaded_files:
                try:
                    cleanup_ok = self._delete_file(
                        fname,
                        client=client,
                        api_key=api_key,
                        cancel_event=context.cancel_event,
                    )
                except GeminiSubtitleError as exc:
                    logger.warning("Gemini remote cleanup failed: %s", type(exc).__name__)
                    cleanup_ok = False
                if not cleanup_ok:
                    warnings.append(
                        {
                            "code": "gemini_remote_cleanup_failed",
                            "message": "Không thể xóa tệp tạm trên Gemini; hãy kiểm tra lại sau.",
                        }
                    )
            client.close()
