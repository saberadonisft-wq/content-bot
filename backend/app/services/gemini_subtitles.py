from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from ..schemas import SubtitleDocumentV2
from .subtitle_timing import validate_cues
from .subtitles import parse_subtitles_v2, subtitles_to_srt


class GeminiSubtitleError(RuntimeError):
    pass


class GeminiSubtitleCanceled(GeminiSubtitleError):
    pass


@dataclass(frozen=True)
class GeminiSubtitleSettings:
    job_root: Path
    cli_path: Path | None = None
    model: str = "auto"
    api_key: str | None = None
    timeout_seconds: int = 1800
    chunk_seconds: int = 180
    max_input_mb: int = 19


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


PROMPT_VERSION = 3


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


class GeminiSubtitleService:
    def __init__(self, settings: GeminiSubtitleSettings) -> None:
        self.settings = settings
        self._auth_issue: str | None = None

    def _resolve_cli_command(self) -> list[str]:
        if self.settings.cli_path:
            configured = self.settings.cli_path.expanduser().resolve()
            if not configured.is_file():
                raise GeminiSubtitleError(f"Không tìm thấy Gemini CLI: {configured}")
            if configured.suffix.lower() == ".ps1":
                powershell = shutil.which("pwsh") or shutil.which("powershell")
                if not powershell:
                    raise GeminiSubtitleError("Không tìm thấy PowerShell để chạy Gemini CLI")
                return [powershell, "-NoProfile", "-File", str(configured)]
            if configured.suffix.lower() in {".js", ".mjs"}:
                node = shutil.which("node")
                if not node:
                    raise GeminiSubtitleError("Không tìm thấy Node.js để chạy Gemini CLI")
                return [node, str(configured)]
            return [str(configured)]

        shim = shutil.which("gemini.cmd") or shutil.which("gemini")
        if shim:
            shim_path = Path(shim).resolve()
            bundle = (
                shim_path.parent
                / "node_modules"
                / "@google"
                / "gemini-cli"
                / "bundle"
                / "gemini.js"
            )
            node = shutil.which("node")
            if bundle.is_file() and node:
                return [node, str(bundle)]
            if shim_path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
                return [str(shim_path)]

        raise GeminiSubtitleError(
            "Gemini CLI chưa được cài. Chạy `npm install -g @google/gemini-cli@latest`, "
            "sau đó cấu hình GEMINI_API_KEY hoặc Vertex AI để chạy chế độ tự động."
        )

    def _configured_api_key(self) -> str | None:
        value = self.settings.api_key or os.environ.get("GEMINI_API_KEY")
        return value.strip() if value and value.strip() else None

    @staticmethod
    def _selected_auth_type() -> str | None:
        settings_path = Path.home() / ".gemini" / "settings.json"
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        security = payload.get("security")
        auth = security.get("auth") if isinstance(security, dict) else None
        selected = auth.get("selectedType") if isinstance(auth, dict) else None
        return selected if isinstance(selected, str) and selected else None

    @staticmethod
    def _windows_oauth_credential_present() -> bool:
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["cmdkey", "/list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return "gemini-cli-oauth/" in result.stdout.lower()

    def _authentication_hint(self) -> tuple[bool, str | None]:
        if self._configured_api_key():
            return True, "gemini_api_key"

        selected = self._selected_auth_type()
        if selected == "gemini-api-key":
            return True, "gemini_api_key"
        if selected in {"vertex-ai", "compute-default-credentials"}:
            return True, "vertex_ai"
        if selected:
            return True, "oauth_personal" if selected == "oauth-personal" else selected

        if (Path.home() / ".gemini" / "oauth_creds.json").is_file():
            return True, "oauth_personal"
        if self._windows_oauth_credential_present():
            return True, "oauth_personal"
        return False, None

    @classmethod
    def _unsupported_client_message(cls) -> str:
        return (
            "Gemini CLI không còn hỗ trợ đăng nhập Google cá nhân (Free/AI Pro/Ultra). "
            "Hãy cấu hình GEMINI_API_KEY/Vertex AI cho chế độ tự động, hoặc dùng tài khoản "
            "Gemini Code Assist tổ chức."
        )

    @classmethod
    def _authentication_required_message(cls) -> str:
        return (
            "Gemini CLI chưa có phương thức xác thực dùng được. Thêm GEMINI_API_KEY vào "
            "backend/.env hoặc cấu hình Vertex AI rồi khởi động lại ứng dụng."
        )

    def status(self) -> dict[str, Any]:
        try:
            self._resolve_cli_command()
        except GeminiSubtitleError:
            return {
                "installed": False,
                "authenticated": False,
                "auth_method": None,
                "issue": "cli_not_installed",
            }
        authenticated, auth_method = self._authentication_hint()
        issue = self._auth_issue
        if issue:
            authenticated = False
        return {
            "installed": True,
            "authenticated": authenticated,
            "auth_method": auth_method,
            "issue": issue,
        }

    def _friendly_cli_error(self, stdout: str, stderr: str) -> str:
        detail = "\n".join(part for part in (stderr, stdout) if part).strip()
        detail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", detail)
        lowered = detail.lower()
        if (
            "ineligibletiererror" in lowered
            or "unsupported_client" in lowered
            or "no longer supported for gemini code assist for individuals" in lowered
        ):
            self._auth_issue = "unsupported_consumer_oauth"
            return self._unsupported_client_message()
        if "api_key_invalid" in lowered or "api key not valid" in lowered:
            self._auth_issue = "invalid_api_key"
            return "GEMINI_API_KEY không hợp lệ hoặc đã bị Google từ chối. Hãy kiểm tra lại key trong backend/.env."
        if any(
            marker in lowered
            for marker in (
                "authenticate",
                "authenticating",
                "authentication",
                "auth method",
                "login required",
                "not logged in",
                "oauth",
            )
        ):
            self._auth_issue = "authentication_required"
            return self._authentication_required_message()
        if "quota" in lowered or "resource_exhausted" in lowered:
            return "Gemini đã hết quota tạm thời; hãy thử lại sau."
        if "file size exceeds" in lowered:
            return "Video proxy vượt giới hạn 20 MB của Gemini CLI."
        return "Gemini CLI không thể xử lý yêu cầu. Kiểm tra cấu hình và thử lại."

    def _run_command(
        self,
        command: list[str],
        *,
        cwd: Path,
        cancel_event: threading.Event,
        timeout_seconds: int | None = None,
        env_overrides: dict[str, str | None] | None = None,
    ) -> CommandResult:
        command_env = {
            **os.environ,
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        for key, value in (env_overrides or {}).items():
            if value is None:
                command_env.pop(key, None)
            else:
                command_env[key] = value
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=command_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + (timeout_seconds or self.settings.timeout_seconds)
        try:
            while True:
                if cancel_event.is_set():
                    raise GeminiSubtitleCanceled("Đã hủy tạo phụ đề bằng Gemini CLI")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GeminiSubtitleError("Gemini CLI xử lý quá thời gian cho phép")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                process.communicate()

        return CommandResult(process.returncode, stdout.strip(), stderr.strip())

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
        result = self._run_command(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start_seconds:.3f}",
                "-i",
                str(video_path.resolve()),
                "-t",
                f"{duration_seconds:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                "scale='min(854,iw)':-2,fps=24",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                f"{video_kbps}k",
                "-maxrate",
                f"{video_kbps}k",
                "-bufsize",
                f"{video_kbps * 2}k",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_kbps}k",
                "-movflags",
                "+faststart",
                "-y",
                str(output_path),
            ],
            cwd=output_path.parent,
            cancel_event=cancel_event,
            timeout_seconds=max(300, round(duration_seconds * 3)),
        )
        if result.returncode != 0 or not output_path.is_file():
            raise GeminiSubtitleError(
                self._friendly_cli_error(result.stdout, result.stderr)
            )
        limit_bytes = self.settings.max_input_mb * 1024 * 1024
        if output_path.stat().st_size > limit_bytes:
            raise GeminiSubtitleError(
                "Không nén được video proxy dưới giới hạn Gemini CLI; "
                "hãy giảm độ dài video hoặc tăng số đoạn chia."
            )

    @staticmethod
    def _prompt(*, bilingual: bool, chunk_index: int, chunk_count: int) -> str:
        secondary_rule = (
            "Đặt nguyên văn ngôn ngữ nguồn trong secondary_text cho từng segment."
            if bilingual
            else "Không thêm secondary_text."
        )
        secondary_example = (
            ',\n      "secondary_text": "Nguyên văn ngôn ngữ nguồn."'
            if bilingual
            else ""
        )
        return f"""@proxy.mp4
Bạn là biên tập viên phụ đề tiếng Việt chuyên nghiệp. Hãy xem và nghe TOÀN BỘ video đính kèm, sau đó tạo phụ đề chính xác.

Đây là đoạn {chunk_index}/{chunk_count}; mọi timestamp phải tính từ 0 của riêng đoạn này.

Ưu tiên bằng chứng theo thứ tự:
1. Đọc phụ đề/chữ hội thoại xuất hiện trên hình nếu có.
2. Đối chiếu với audio, nhân vật và ngữ cảnh hình ảnh.
3. Giữ tên nhân vật, đại từ và thuật ngữ nhất quán trong toàn đoạn.

Nếu video có phụ đề gốc hiển thị trên hình:
- Phụ đề gốc là transcript và timing anchor ưu tiên cao nhất.
- Bám sát từng cue gốc: giữ nguyên thứ tự, điểm bắt đầu, điểm kết thúc, cách xuống dòng và ranh giới lượt thoại.
- Mỗi cue dịch tiếng Việt phải xuất hiện đồng thời với cue gốc tương ứng. Không gộp nhiều cue gốc thành một cue dịch và không tự tách một cue gốc thành nhiều cue dịch nếu không có bằng chứng rõ ràng.
- Đặt nguyên văn nội dung gốc vào secondary_text khi chế độ song ngữ được bật để làm metadata đối chiếu/alignment; trường này không phải dòng cần hiển thị trên video. Đặt bản dịch tiếng Việt vào text.
- Nếu phụ đề gốc và audio có vẻ lệch, ưu tiên thời điểm phụ đề thực sự xuất hiện trên hình, đặt needs_review=true và giảm confidence; không tự nối các cue thành một dải liên tục.

Nếu video không có phụ đề gốc nhìn thấy:
- Dùng audio làm nguồn timing chính.
- start_ms là âm đầu tiên nghe được và end_ms là sau âm cuối cùng nghe được; giữ nguyên khoảng im lặng giữa hai lượt thoại.

Yêu cầu dịch:
- Dịch tự nhiên sang tiếng Việt, đúng ý và đúng sắc thái; không dịch từng chữ máy móc.
- Không để sót chữ Trung/Nhật/Hàn trong dòng tiếng Việt; tên riêng phải phiên âm nhất quán.
- Không bịa lời khi không nghe/đọc rõ.
- {secondary_rule}

Yêu cầu timing:
- Mỗi cue là một câu/ý tự nhiên, thường 1–6 giây và không quá 84 ký tự tiếng Việt.
- start_ms/end_ms là số nguyên mili-giây, 0 <= start_ms < end_ms.
- Khi có phụ đề gốc trên hình, start_ms/end_ms phải bám theo thời gian xuất hiện của cue gốc để bản dịch xuất hiện cùng lúc.
- Khi không có phụ đề gốc, không làm tròn tất cả cue về giây tròn và không kéo cue kế tiếp sát cue trước; nếu audio có khoảng nghỉ thì phải giữ gap thật.
- Cue theo thứ tự và không chồng lấn, trừ khi có hai người thực sự nói đồng thời.
- Không đặt timing_precision_ms=10 chỉ để tạo vẻ chính xác. Dùng 1000 nếu chỉ chắc đến từng giây, 100 nếu có bằng chứng gần 0,1 giây; bước alignment audio phía sau mới lượng tử hóa về 10 ms.

CHỈ trả về một JSON hợp lệ, không Markdown, không giải thích:
{{
  "schema_version": 2,
  "language": "vi",
  "timebase": "milliseconds",
  "timing_source": "gemini_estimate",
  "timing_precision_ms": 100,
  "segments": [
    {{
      "id": "g0001",
      "start_ms": 0,
      "end_ms": 2500,
      "text": "Phụ đề tiếng Việt."{secondary_example},
      "needs_review": false
    }}
  ]
}}"""

    def _extract_response(self, output: str) -> tuple[str, dict[str, Any]]:
        try:
            envelope = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GeminiSubtitleError("Gemini CLI trả về envelope JSON không hợp lệ") from exc
        if not isinstance(envelope, dict):
            raise GeminiSubtitleError("Gemini CLI trả về dữ liệu không đúng định dạng")
        response = envelope.get("response")
        if not isinstance(response, str) or not response.strip():
            error = envelope.get("error")
            if error:
                detail = (
                    json.dumps(error, ensure_ascii=False)
                    if isinstance(error, (dict, list))
                    else str(error)
                )
                raise GeminiSubtitleError(self._friendly_cli_error("", detail))
            raise GeminiSubtitleError("Gemini CLI không trả về nội dung")
        return response, envelope

    def _run_gemini(
        self,
        workspace: Path,
        *,
        prompt: str,
        cancel_event: threading.Event,
    ) -> tuple[str, dict[str, Any]]:
        self._auth_issue = None
        env_overrides: dict[str, str | None] = {}
        api_key = self._configured_api_key()
        if api_key:
            # Keep the user's global Gemini CLI auth untouched. A workspace-level
            # setting lets the headless child use the API key even when the user
            # previously selected the retired personal OAuth method.
            auth_settings_dir = workspace / ".gemini"
            auth_settings_dir.mkdir(parents=True, exist_ok=True)
            (auth_settings_dir / "settings.json").write_text(
                json.dumps(
                    {"security": {"auth": {"selectedType": "gemini-api-key"}}},
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            env_overrides = {"GEMINI_API_KEY": api_key}
        command = [
            *self._resolve_cli_command(),
            "--prompt",
            prompt,
            "--output-format",
            "json",
            "--skip-trust",
        ]
        if self.settings.model and self.settings.model != "auto":
            command.extend(["--model", self.settings.model])
        result = self._run_command(
            command,
            cwd=workspace,
            cancel_event=cancel_event,
            env_overrides=env_overrides,
        )
        if result.returncode != 0:
            raise GeminiSubtitleError(
                self._friendly_cli_error(result.stdout, result.stderr)
            )
        return self._extract_response(result.stdout)

    @staticmethod
    def _offset_cues(
        cues: list[dict[str, Any]],
        *,
        offset_ms: int,
        duration_ms: int,
        existing_count: int,
    ) -> list[dict[str, Any]]:
        shifted: list[dict[str, Any]] = []
        for index, cue in enumerate(cues):
            start_ms = min(duration_ms - 1, max(0, int(cue["start_ms"]) + offset_ms))
            end_ms = min(duration_ms, max(start_ms + 1, int(cue["end_ms"]) + offset_ms))
            next_cue = {
                **cue,
                "id": f"gm-{existing_count + index + 1:04d}-{start_ms}",
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
        self._resolve_cli_command()
        authenticated, _ = self._authentication_hint()
        if self._auth_issue:
            authenticated = False
        if not authenticated:
            if self._auth_issue == "unsupported_consumer_oauth":
                raise GeminiSubtitleError(self._unsupported_client_message())
            if self._auth_issue == "invalid_api_key":
                raise GeminiSubtitleError(
                    "GEMINI_API_KEY không hợp lệ hoặc đã bị Google từ chối. Hãy kiểm tra lại key trong backend/.env."
                )
            raise GeminiSubtitleError(self._authentication_required_message())
        duration_ms = int(media["duration_ms"])
        workspace = (self.settings.job_root / context.job_id).resolve()
        job_root = self.settings.job_root.resolve()
        if not workspace.is_relative_to(job_root):
            raise GeminiSubtitleError("Thư mục job Gemini không hợp lệ")
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        chunk_ms = max(30_000, self.settings.chunk_seconds * 1000)
        chunk_count = max(1, math.ceil(duration_ms / chunk_ms))
        combined_cues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        envelopes: list[dict[str, Any]] = []
        try:
            for chunk_index in range(chunk_count):
                context.raise_if_canceled()
                offset_ms = chunk_index * chunk_ms
                current_duration_ms = min(chunk_ms, duration_ms - offset_ms)
                progress = 5 + round((chunk_index / chunk_count) * 85)
                context.update(
                    progress,
                    "preparing_video",
                    f"Đang chuẩn bị video cho Gemini ({chunk_index + 1}/{chunk_count})",
                )
                chunk_dir = workspace / f"chunk-{chunk_index + 1:03d}"
                chunk_dir.mkdir(parents=True, exist_ok=True)
                proxy_path = chunk_dir / "proxy.mp4"
                can_use_original = (
                    chunk_count == 1
                    and video_path.suffix.lower() == ".mp4"
                    and video_path.stat().st_size
                    <= self.settings.max_input_mb * 1024 * 1024
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
                    min(90, progress + 8),
                    "gemini_analyzing",
                    f"Gemini đang xem và dịch đoạn {chunk_index + 1}/{chunk_count}",
                )
                response, envelope = self._run_gemini(
                    chunk_dir,
                    prompt=self._prompt(
                        bilingual=bool(options.get("bilingual", True)),
                        chunk_index=chunk_index + 1,
                        chunk_count=chunk_count,
                    ),
                    cancel_event=context.cancel_event,
                )
                document, chunk_warnings = parse_subtitles_v2(
                    response,
                    media_duration_ms=current_duration_ms,
                )
                if not document["segments"]:
                    raise GeminiSubtitleError(
                        f"Gemini không trả về cue hợp lệ cho đoạn {chunk_index + 1}"
                    )
                shifted = self._offset_cues(
                    document["segments"],
                    offset_ms=offset_ms,
                    duration_ms=duration_ms,
                    existing_count=len(combined_cues),
                )
                combined_cues.extend(shifted)
                warnings.extend(chunk_warnings)
                envelopes.append(envelope)
                if len(combined_cues) > 500:
                    raise GeminiSubtitleError("Gemini trả về quá 500 cue phụ đề")

            context.update(95, "validating_result", "Đang kiểm tra phụ đề Gemini")
            document_precision = max(
                100,
                *(int(cue.get("timing_precision_ms", 100)) for cue in combined_cues),
            )
            document = SubtitleDocumentV2(
                language="vi",
                timing_source="gemini_estimate",
                timing_precision_ms=document_precision,
                segments=combined_cues,
            ).model_dump(mode="json")
            warnings.extend(validate_cues(document["segments"]))
            return {
                "document": document,
                "warnings": warnings,
                "srt": subtitles_to_srt(document["segments"]),
                "segment_count": len(combined_cues),
                "processing_seconds": round(time.monotonic() - started, 3),
                "provider": "gemini_cli",
                "model": self.settings.model,
                "chunk_count": chunk_count,
                "usage": [envelope.get("stats", {}) for envelope in envelopes],
            }
        finally:
            if workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)
