from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from app import main
from app.schemas import GeminiSubtitleRequest, SubtitleJobResponse
from app.services.gemini_subtitles import (
    CommandResult,
    GeminiSubtitleService,
    GeminiSubtitleSettings,
)
from app.services.subtitle_jobs import SubtitleJobManager


def _service(tmp_path: Path) -> GeminiSubtitleService:
    return GeminiSubtitleService(
        GeminiSubtitleSettings(job_root=tmp_path, model="auto")
    )


def test_subprocess_forces_utf8_for_vietnamese_output(tmp_path: Path) -> None:
    result = _service(tmp_path)._run_command(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.stdout.encoding); print('Đang tạo phụ đề tiếng Việt')",
        ],
        cwd=tmp_path,
        cancel_event=threading.Event(),
    )

    assert result.stdout.splitlines() == ["utf-8", "Đang tạo phụ đề tiếng Việt"]


def test_extract_and_offset_gemini_response(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "response": json.dumps(
                {
                    "schema_version": 2,
                    "language": "vi",
                    "timebase": "milliseconds",
                    "timing_source": "gemini_estimate",
                    "timing_precision_ms": 100,
                    "segments": [],
                }
            ),
            "stats": {"models": {"auto": {"requests": 1}}},
        }
    )
    content, envelope = _service(tmp_path)._extract_response(response)
    shifted = _service(tmp_path)._offset_cues(
        [
            {
                "id": "g1",
                "start_ms": 200,
                "end_ms": 1200,
                "text": "Xin chào",
                "timing_source": "gemini_estimate",
                "timing_precision_ms": 100,
            }
        ],
        offset_ms=3000,
        duration_ms=5000,
        existing_count=2,
    )

    assert json.loads(content)["schema_version"] == 2
    assert envelope["stats"]["models"]
    assert shifted[0]["id"] == "gm-0003-3200"
    assert shifted[0]["start_ms"] == 3200
    assert shifted[0]["end_ms"] == 4200


def test_unsupported_consumer_oauth_error_is_safe_and_actionable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    raw_error = """
Error authenticating: IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals.
reasonCode: UNSUPPORTED_CLIENT
    at throwIneligibleOrProjectIdError (file:///C:/Users/vhc/AppData/Roaming/npm/node_modules/gemini.js:309966:11)
Ripgrep is not available. Falling back to GrepTool.
An unexpected critical error occurred: IneligibleTierError
"""

    message = service._friendly_cli_error("", raw_error)

    assert "không còn hỗ trợ" in message
    assert "GEMINI_API_KEY" in message
    assert "file:///" not in message
    assert "Ripgrep" not in message
    assert "node_modules" not in message


def test_extract_response_sanitizes_zero_exit_error_envelope(tmp_path: Path) -> None:
    service = _service(tmp_path)
    output = json.dumps(
        {
            "error": {
                "name": "IneligibleTierError",
                "reasonCode": "UNSUPPORTED_CLIENT",
                "message": "This client is no longer supported for Gemini Code Assist for individuals",
            }
        }
    )

    try:
        service._extract_response(output)
    except Exception as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected the error envelope to fail")

    assert "GEMINI_API_KEY" in message
    assert "reasonCode" not in message


def test_api_key_uses_workspace_auth_override_without_mutating_global_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = GeminiSubtitleService(
        GeminiSubtitleSettings(job_root=tmp_path, api_key="test-api-key")
    )
    captured: dict[str, object] = {}

    def fake_run_command(command, *, cwd, cancel_event, env_overrides=None, **_kwargs):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env_overrides"] = env_overrides
        return CommandResult(
            0,
            json.dumps({"response": "{}"}),
            "",
        )

    monkeypatch.setattr(service, "_run_command", fake_run_command)
    service._run_gemini(
        tmp_path,
        prompt="prompt",
        cancel_event=threading.Event(),
    )

    auth_settings = json.loads(
        (tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8")
    )
    assert auth_settings["security"]["auth"]["selectedType"] == "gemini-api-key"
    assert captured["env_overrides"] == {"GEMINI_API_KEY": "test-api-key"}


def test_prompt_only_requests_secondary_text_for_bilingual_mode(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    bilingual_prompt = service._prompt(
        bilingual=True,
        chunk_index=1,
        chunk_count=1,
    )
    vietnamese_only_prompt = service._prompt(
        bilingual=False,
        chunk_index=1,
        chunk_count=1,
    )

    assert '"secondary_text"' in bilingual_prompt
    assert '"secondary_text"' not in vietnamese_only_prompt
    assert "Không thêm secondary_text." in vietnamese_only_prompt


def test_gemini_endpoint_runs_as_separate_attachable_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fixture")
    manager = SubtitleJobManager(tmp_path / "jobs", max_workers=1)
    media = {
        "fingerprint": "1" * 64,
        "audio_hash": "2" * 64,
        "duration_ms": 5000,
        "has_audio": True,
    }
    monkeypatch.setattr(main, "gemini_subtitle_jobs", manager)
    monkeypatch.setattr(main, "_uploaded_video_path", lambda _video_id: video_path)
    monkeypatch.setattr(main, "probe_media_cached", lambda *_args, **_kwargs: media)

    def fake_generate(_path, _media, _options, context):
        context.update(70, "gemini_analyzing", "Gemini Pro đang xem video")
        return {
            "document": {
                "schema_version": 2,
                "language": "vi",
                "timebase": "milliseconds",
                "timing_source": "gemini_estimate",
                "timing_precision_ms": 100,
                "segments": [],
            },
            "warnings": [],
            "srt": "",
            "segment_count": 0,
            "provider": "gemini_cli",
            "model": "auto",
            "chunk_count": 1,
        }

    monkeypatch.setattr(
        main,
        "gemini_subtitle_service",
        SimpleNamespace(generate=fake_generate),
    )
    submitted = main.generate_subtitles_with_gemini_endpoint(
        GeminiSubtitleRequest(video_id="a" * 12)
    )
    validated_submission = SubtitleJobResponse(**submitted)
    assert validated_submission.kind == "generation"

    deadline = time.monotonic() + 3
    completed = None
    while time.monotonic() < deadline:
        completed = manager.get(validated_submission.id)
        if completed and completed["state"] == "succeeded":
            break
        time.sleep(0.01)

    assert completed is not None
    validated = SubtitleJobResponse(**completed)
    assert validated.state == "succeeded"
    assert validated.result is not None
    assert validated.result["provider"] == "gemini_cli"
    manager.shutdown()
