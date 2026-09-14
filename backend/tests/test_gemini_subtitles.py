from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from app.api import subtitles as subtitles_api
from app.schemas import GeminiSubtitleRequest, SubtitleJobResponse
from app.services.gemini_subtitles import (
    GeminiSubtitleError,
    GeminiSubtitleService,
    GeminiSubtitleSettings,
)
from app.services.subtitle_jobs import SubtitleJobManager


def _service(tmp_path: Path, api_key: str = "test-key") -> GeminiSubtitleService:
    return GeminiSubtitleService(
        GeminiSubtitleSettings(job_root=tmp_path, api_key=api_key)
    )


def test_status_no_api_key(tmp_path: Path) -> None:
    svc = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path, api_key=None))
    st = svc.status()
    assert st["authenticated"] is False
    assert st["issue"] == "no_api_key"
    assert st["provider"] == "api_direct"


def test_status_with_api_key(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    st = svc.status()
    assert st["authenticated"] is True
    assert st["auth_method"] == "gemini_api_key"
    assert st["provider"] == "api_direct"
    assert st["model"] == "gemini-3.6-flash"


def test_offset_cues(tmp_path: Path) -> None:
    shifted = _service(tmp_path)._offset_cues(
        [
            {
                "id": "g1",
                "start_ms": 200,
                "end_ms": 1200,
                "text": "Xin chao",
                "timing_source": "gemini_estimate",
                "timing_precision_ms": 100,
            }
        ],
        offset_ms=3000,
        chunk_duration_ms=2000,
        duration_ms=5000,
        existing_count=2,
    )
    assert shifted[0]["id"] == "gm-0003-3200"
    assert shifted[0]["start_ms"] == 3200
    assert shifted[0]["end_ms"] == 4200


def test_offset_cues_drops_content_after_chunk_end_and_clips_crossing_cue(
    tmp_path: Path,
) -> None:
    shifted = _service(tmp_path)._offset_cues(
        [
            {"id": "valid", "start_ms": 800, "end_ms": 1200, "text": "Hop le"},
            {"id": "crossing", "start_ms": 1800, "end_ms": 2600, "text": "Bi cat"},
            {"id": "invented", "start_ms": 2400, "end_ms": 3000, "text": "Ngoai video"},
        ],
        offset_ms=3000,
        chunk_duration_ms=2000,
        duration_ms=5000,
        existing_count=0,
    )

    assert [(cue["start_ms"], cue["end_ms"]) for cue in shifted] == [
        (3800, 4200),
        (4800, 5000),
    ]
    assert [cue["id"] for cue in shifted] == ["gm-0001-3800", "gm-0002-4800"]


def test_prompt_bilingual_has_secondary_text(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    prompt_bi = svc._build_prompt(
        bilingual=True, chunk_index=1, chunk_count=1, chunk_duration_ms=60000
    )
    prompt_vi = svc._build_prompt(
        bilingual=False, chunk_index=1, chunk_count=1, chunk_duration_ms=60000
    )
    assert "secondary_text" in prompt_bi
    example = json.loads(prompt_vi[prompt_vi.index('{\n  "schema_version"'):])
    assert "secondary_text" not in example["segments"][0]
    assert example["segments"][0]["source_text"]
    assert "VIDEO_END_MS=60000" in prompt_bi
    assert "0 <= start_ms < end_ms <= 60000" in prompt_bi
    assert "Do not complete cut-off sentences" in prompt_bi
    assert "Return zero segments" in prompt_bi


def test_generate_content_retries_on_429(tmp_path: Path) -> None:
    svc = _service(tmp_path, api_key="fake-key")
    attempts = []
    status_messages: list[str] = []

    def mock_send(request, **kwargs):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(429, json={"error": {"message": "Rate limit"}})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"schema_version": 2, "language": "vi", "timebase": "milliseconds", "timing_source": "gemini_estimate", "timing_precision_ms": 100, "segments": []}'
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(mock_send)
    with (
        mock.patch.object(
            svc,
            "_client",
            lambda: httpx.Client(
                transport=transport,
                base_url="https://generativelanguage.googleapis.com",
            ),
        ),
        mock.patch(
            "app.services.gemini_subtitles._interruptible_sleep", return_value=None
        ),
    ):
        result = svc._generate_content(
            "files/test123",
            "test prompt",
            cancel_event=threading.Event(),
            status_callback=status_messages.append,
        )
    assert len(attempts) == 3
    assert "schema_version" in result
    assert len(status_messages) == 2
    assert "HTTP 429" in status_messages[0]
    assert "Rate limit" in status_messages[0]
    assert "thử lại sau 3s (1/5)" in status_messages[0]
    assert "thử lại sau 3s (2/5)" in status_messages[1]


def test_retry_delay_is_fixed_at_three_seconds_unless_server_requests_longer(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path)

    assert svc._retry_delay(1) == 3
    assert svc._retry_delay(5) == 3
    assert svc._retry_delay(1, httpx.Response(503, headers={"Retry-After": "30"})) == 30


def test_generate_retry_status_distinguishes_5xx_from_rate_limit(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path, api_key="fake-key")
    attempts = 0
    status_messages: list[str] = []

    def mock_send(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                502,
                json={"error": {"message": "Bad gateway"}},
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}
                ]
            },
        )

    with (
        mock.patch.object(
            svc,
            "_client",
            lambda: httpx.Client(
                transport=httpx.MockTransport(mock_send),
                base_url="https://generativelanguage.googleapis.com",
            ),
        ),
        mock.patch(
            "app.services.gemini_subtitles._interruptible_sleep", return_value=None
        ),
    ):
        svc._generate_content(
            "files/test123",
            "test prompt",
            cancel_event=threading.Event(),
            status_callback=status_messages.append,
        )

    assert len(status_messages) == 1
    assert "HTTP 502" in status_messages[0]
    assert "Bad gateway" in status_messages[0]
    assert "HTTP 429" not in status_messages[0]


@pytest.mark.parametrize(
    ("error_type", "expected_message"),
    [
        (httpx.ReadTimeout, "Hết thời gian chờ Gemini khi phân tích video"),
        (httpx.ConnectError, "Mất kết nối tới Gemini khi phân tích video"),
    ],
)
def test_generate_retry_status_distinguishes_timeout_and_connection_error(
    tmp_path: Path,
    error_type: type[httpx.TransportError],
    expected_message: str,
) -> None:
    svc = _service(tmp_path, api_key="fake-key")
    attempts = 0
    status_messages: list[str] = []

    def mock_send(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type("temporary failure", request=request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}
                ]
            },
        )

    with (
        mock.patch.object(
            svc,
            "_client",
            lambda: httpx.Client(
                transport=httpx.MockTransport(mock_send),
                base_url="https://generativelanguage.googleapis.com",
            ),
        ),
        mock.patch(
            "app.services.gemini_subtitles._interruptible_sleep", return_value=None
        ),
    ):
        svc._generate_content(
            "files/test123",
            "test prompt",
            cancel_event=threading.Event(),
            status_callback=status_messages.append,
        )

    assert status_messages == [f"{expected_message}; thử lại sau 3s (1/5)"]


def test_fallback_does_not_call_model_already_in_daily_cooldown(tmp_path: Path) -> None:
    from app.services.gemini_dispatch import ApiFailure
    from app.services.gemini_subtitles import GeminiApiError
    svc = _service(tmp_path, api_key="fake-key")
    owner = svc.runtime_keys()[0]
    svc.dispatcher.report(owner, "gemini-3.6-flash", ApiFailure(429, "generateContent", "quota", 86400, "daily"))
    models = []
    def send(request):
        models.append(request.url.path.rsplit('/', 1)[-1].split(':')[0])
        return httpx.Response(503, json={"error": {"message": "overloaded"}})
    with httpx.Client(transport=httpx.MockTransport(send), base_url="https://generativelanguage.googleapis.com") as client, pytest.raises(GeminiApiError) as error:
        svc._generate_content("files/test", "prompt", model="gemini-3.8-flash", client=client,
            cancel_event=threading.Event(), model_guard=lambda model: svc._guard_model(owner, model))
    assert error.value.failure.category == 'quota'
    assert models == ['gemini-3.8-flash', 'gemini-3.7-flash']


def test_generation_joins_output_parts_and_skips_thoughts(tmp_path: Path) -> None:
    svc = _service(tmp_path, api_key="fake-key")
    schema = {"type": "object", "required": ["segments"]}
    def send(request):
        payload = json.loads(request.content)
        assert payload["generationConfig"]["responseSchema"] == {"type": "OBJECT", "required": ["segments"]}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"thought": True, "text": '{"analysis": "not output"}'},
            {"text": '{"segments":'}, {"text": '[]}'},
        ]}}], "usageMetadata": {"totalTokenCount": 10, "unrecognized": "not retained"}})
    with httpx.Client(transport=httpx.MockTransport(send), base_url="https://generativelanguage.googleapis.com") as client:
        execution = {}
        raw = svc._generate_content("files/test", "prompt", client=client, api_key="fake-key", cancel_event=threading.Event(), response_schema=schema, execution=execution)
    assert json.loads(raw) == {"segments": []}
    assert execution["usage"] == {"totalTokenCount": 10}


def test_gemini_uses_api_key_header_and_not_query_parameter(tmp_path: Path) -> None:
    svc = _service(tmp_path, api_key="header-key")
    requests: list[httpx.Request] = []

    def mock_send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}
                ]
            },
        )

    transport = httpx.MockTransport(mock_send)
    with mock.patch.object(
        svc,
        "_client",
        lambda: httpx.Client(
            transport=transport,
            base_url="https://generativelanguage.googleapis.com",
        ),
    ):
        svc._generate_content(
            "files/test123",
            "test prompt",
            cancel_event=threading.Event(),
            model="gemini-3.6-flash",
        )

    assert requests[0].headers["x-goog-api-key"] == "header-key"
    assert "key" not in requests[0].url.query.decode()
    assert requests[0].url.path.endswith("/models/gemini-3.6-flash:generateContent")


def test_list_models_uses_header_and_filters_generation_models(tmp_path: Path) -> None:
    svc = _service(tmp_path, api_key="list-key")
    requests: list[httpx.Request] = []

    def mock_send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-3.6-flash",
                        "baseModelId": "gemini-3.6-flash",
                        "displayName": "Gemini 3.6 Flash",
                        "description": "Fast multimodal model",
                        "supportedGenerationMethods": ["generateContent"],
                        "inputTokenLimit": 1_000_000,
                        "outputTokenLimit": 65_536,
                    },
                    {
                        "name": "models/text-embedding-005",
                        "baseModelId": "text-embedding-005",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )

    with mock.patch.object(
        svc,
        "_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(mock_send),
            base_url="https://generativelanguage.googleapis.com",
        ),
    ):
        models = svc.list_models()

    assert len(requests) == 1
    assert requests[0].headers["x-goog-api-key"] == "list-key"
    assert "key" not in requests[0].url.query.decode()
    assert [model["id"] for model in models] == ["gemini-3.6-flash"]
    assert models[0]["display_name"] == "Gemini 3.6 Flash"


def test_upload_streams_proxy_file_and_reuses_api_key_header(tmp_path: Path) -> None:
    svc = _service(tmp_path, api_key="upload-key")
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"proxy-bytes")
    uploads: list[bytes] = []

    def mock_send(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/v1beta/files":
            return httpx.Response(
                200,
                headers={"X-Goog-Upload-URL": "https://upload.test/finalize"},
            )
        if request.url.host == "upload.test":
            uploads.append(request.read())
            return httpx.Response(200, json={"file": {"name": "files/test123"}})
        return httpx.Response(200, json={"state": "ACTIVE"})

    client = httpx.Client(
        transport=httpx.MockTransport(mock_send),
        base_url="https://generativelanguage.googleapis.com",
    )
    try:
        file_name = svc._upload_file(
            proxy,
            cancel_event=threading.Event(),
            client=client,
            api_key="upload-key",
        )
    finally:
        client.close()

    assert file_name == "files/test123"
    assert uploads == [b"proxy-bytes"]


def test_upload_retry_status_reports_operation_and_http_code(tmp_path: Path) -> None:
    svc = _service(tmp_path, api_key="upload-key")
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"proxy-bytes")
    init_attempts = 0
    status_messages: list[str] = []

    def mock_send(request: httpx.Request) -> httpx.Response:
        nonlocal init_attempts
        if request.url.path == "/upload/v1beta/files":
            init_attempts += 1
            if init_attempts == 1:
                return httpx.Response(
                    429,
                    json={"error": {"message": "Upload quota exceeded"}},
                )
            return httpx.Response(
                200,
                headers={"X-Goog-Upload-URL": "https://upload.test/finalize"},
            )
        if request.url.host == "upload.test":
            return httpx.Response(200, json={"file": {"name": "files/test123"}})
        return httpx.Response(200, json={"state": "ACTIVE"})

    client = httpx.Client(
        transport=httpx.MockTransport(mock_send),
        base_url="https://generativelanguage.googleapis.com",
    )
    try:
        with mock.patch(
            "app.services.gemini_subtitles._interruptible_sleep",
            return_value=None,
        ):
            svc._upload_file(
                proxy,
                cancel_event=threading.Event(),
                status_callback=status_messages.append,
                client=client,
                api_key="upload-key",
            )
    finally:
        client.close()

    assert status_messages == [
        (
            "Gemini giới hạn tốc độ/quota (HTTP 429) khi khởi tạo upload: "
            "Upload quota exceeded; thử lại sau 3s (1/5)"
        )
    ]


def test_remote_cleanup_failure_is_reported_as_warning(
    tmp_path: Path, monkeypatch
) -> None:
    svc = _service(tmp_path, api_key="header-key")
    from app.services.gemini_media import ChunkPolicy, plan_chunks
    monkeypatch.setattr("app.services.gemini_pipeline.prepare_manifest", lambda *a, **k: plan_chunks(5000, [], ChunkPolicy(), fingerprint="fixture"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    media = {"duration_ms": 5000, "audio_hash": "a" * 64, "fingerprint": "b" * 64}
    context = SimpleNamespace(
        job_id="job001",
        cancel_event=threading.Event(),
        raise_if_canceled=lambda: None,
        update=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(svc, "_upload_file", lambda *args, **kwargs: "files/test123")
    monkeypatch.setattr(
        svc,
        "_generate_content",
        lambda *args, **kwargs: (
            '{"schema_version":2,"language":"vi","timebase":"milliseconds","timing_source":"gemini_estimate","timing_precision_ms":100,"segments":[{"id":"g1","start_ms":0,"end_ms":1000,"text":"Xin chào","source_text":"你好","source_language":"zh","content_source":"screen"}]}'
        ),
    )
    monkeypatch.setattr(svc, "_audit_content", lambda *args, **kwargs: json.dumps({
        "reviewed_entire_clip": True, "issues": [], "units": [{"start_ms": 0, "end_ms": 1000,
        "source_text": "你好", "candidate_indices": [0], "translation_ok": True,
        "evidence": "Fixture visible source at 0-1000 ms"}]}))
    monkeypatch.setattr(svc, "_delete_file", lambda *args, **kwargs: False)

    result = svc.generate(video, media, {}, context)

    assert any(
        warning["code"] == "gemini_remote_cleanup_failed"
        for warning in result["warnings"]
    )


def test_generate_raises_after_max_retries(tmp_path: Path) -> None:
    svc = _service(tmp_path, api_key="fake-key")

    def always_429(request, **kwargs):
        return httpx.Response(429, json={"error": {"message": "Rate limit"}})

    transport = httpx.MockTransport(always_429)
    with (
        mock.patch.object(
            svc,
            "_client",
            lambda: httpx.Client(
                transport=transport,
                base_url="https://generativelanguage.googleapis.com",
            ),
        ),
        mock.patch(
            "app.services.gemini_subtitles._interruptible_sleep", return_value=None
        ),
        pytest.raises(GeminiSubtitleError, match="HTTP 429"),
    ):
        svc._generate_content(
            "files/test123",
            "test prompt",
            cancel_event=threading.Event(),
        )


def test_generate_raises_on_missing_api_key(tmp_path: Path) -> None:
    svc = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path, api_key=None))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    media = {"duration_ms": 5000, "audio_hash": "a" * 64, "fingerprint": "b" * 64}
    ctx = SimpleNamespace(
        job_id="job001",
        cancel_event=threading.Event(),
        raise_if_canceled=lambda: None,
        update=lambda *a, **kw: None,
    )
    with pytest.raises(GeminiSubtitleError, match="API key"):
        svc.generate(video, media, {}, ctx)


def test_gemini_endpoint_runs_as_separate_attachable_job(
    application_services,
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
    monkeypatch.setattr(application_services, "gemini_subtitle_jobs", manager)
    monkeypatch.setattr(subtitles_api, "_uploaded_video_path", lambda _video_id: video_path)
    monkeypatch.setattr(subtitles_api, "probe_media_cached", lambda *_args, **_kwargs: media)

    def fake_generate(_path, _media, _options, context):
        context.update(70, "gemini_analyzing", "Gemini dang xem video")
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
            "provider": "gemini_api",
            "model": "gemini-3.6-flash",
            "chunk_count": 1,
        }

    monkeypatch.setattr(
        application_services,
        "gemini_subtitle_service",
        SimpleNamespace(generate=fake_generate, cache_policy=lambda: {"pipeline": 1}),
    )
    submitted = subtitles_api.generate_subtitles_with_gemini_endpoint(
        GeminiSubtitleRequest(video_id="a" * 12), services=application_services
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
    assert validated.result["provider"] == "gemini_api"
    manager.shutdown()
