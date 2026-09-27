"""Exercise real dispatcher/HTTP code against an in-memory transport, never Gemini."""

import json
import threading
import time

import httpx
import pytest

from app.schemas import SubtitleCueV2, SubtitleDocumentV2
from app.services.gemini_subtitles import (
    GeminiSubtitleError,
    GeminiSubtitleService,
    GeminiSubtitleSettings,
)
from app.services.subtitle_translate import (
    SubtitleTranslateError,
    translate_source_document,
)


def document():
    return SubtitleDocumentV2(
        document_role="source",
        language="en",
        revision=3,
        run_id="source-fixture",
        segments=[
            SubtitleCueV2(
                id="c1",
                start_ms=100,
                end_ms=900,
                text="corrected first",
                source_text="stale first",
            ),
            SubtitleCueV2(id="c2", start_ms=1500, end_ms=2200, text="second"),
        ],
    )


def response(text):
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 8,
                "totalTokenCount": 18,
            },
        },
    )


def make_service(tmp_path, monkeypatch, handler, *, retries=0, keys=None):
    service = GeminiSubtitleService(
        GeminiSubtitleSettings(
            job_root=tmp_path, api_key="fixture", max_retries=retries
        ),
        keyring_provider=(lambda: keys) if keys else None,
    )
    monkeypatch.setattr(
        service,
        "_client",
        lambda: httpx.Client(
            base_url="https://fixture.invalid", transport=httpx.MockTransport(handler)
        ),
    )
    return service


def batch_of(request):
    parts = json.loads(request.content)["contents"][0]["parts"]
    assert all("file_data" not in part for part in parts)
    return json.loads(
        parts[0]["text"]
        .split("BATCH: ", 1)[1]
        .split("\nFollowing source context", 1)[0]
    )


def test_resume_reads_completed_batch_and_translates_corrected_source(
    tmp_path, monkeypatch
):
    calls = []
    fail = [True]

    def handler(request):
        items = batch_of(request)
        calls.append([item["id"] for item in items])
        if calls[-1] == ["c2"] and fail[0]:
            return httpx.Response(500, json={"error": {"message": "fixture transient"}})
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": "vi " + item["source_text"]}
                        for item in items
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler)
    source = document()
    with pytest.raises(SubtitleTranslateError) as caught:
        translate_source_document(
            service, source, batch_size=1, cache_dir=tmp_path / "cache"
        )
    assert caught.value.partial_result["translated_count"] == 1
    assert caught.value.partial_result["untranslated_ids"] == ["c2"]
    assert len(caught.value.partial_result["document"]["segments"]) == 1
    fail[0] = False
    result = translate_source_document(
        service, source, batch_size=1, cache_dir=tmp_path / "cache", bilingual=False
    )
    assert calls == [["c1"], ["c2"], ["c2"]]
    assert result["cache_hits"] == 1
    assert result["document"]["source_revision"] == 3
    assert result["document"]["source_run_id"] == "source-fixture"
    assert result["document"]["segments"][0]["text"] == "vi corrected first"
    assert source.segments[0].source_text == "stale first"  # Input is never mutated.
    assert [
        (cue["id"], cue["start_ms"], cue["end_ms"])
        for cue in result["document"]["segments"]
    ] == [("c1", 100, 900), ("c2", 1500, 2200)]
    assert all(cue["secondary_text"] is None for cue in result["document"]["segments"])


def test_expired_deadline_sends_no_request(tmp_path, monkeypatch):
    calls = []
    service = make_service(
        tmp_path, monkeypatch, lambda request: calls.append(request) or response("{}")
    )
    with pytest.raises(GeminiSubtitleError, match="thoi gian"):
        service._generate_content_direct(
            "fixture", cancel_event=threading.Event(), deadline=time.monotonic() - 1
        )
    assert calls == []


def test_text_uses_shared_fallback_and_records_actual_model(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "fixture overload"}})
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": "translated"}
                        for item in batch_of(request)
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler, retries=2)
    result = translate_source_document(service, document(), model="gemini-3.8-flash")
    assert [url.split("/")[-1] for url in calls] == [
        "gemini-3.8-flash:generateContent",
        "gemini-3.7-flash:generateContent",
    ]
    assert result["document"]["translation_models"] == ["gemini-3.7-flash"]
    assert result["batch_provenance"][0]["usage"]["totalTokenCount"] == 18


def test_last_model_overload_retries_then_completes(tmp_path, monkeypatch):
    calls = []
    waits = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503)
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": "translated"}
                        for item in batch_of(request)
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler, retries=2)
    monkeypatch.setattr(
        threading.Event, "wait", lambda self, delay: waits.append(delay) or False
    )
    result = translate_source_document(service, document(), model="gemini-3.6-flash")
    assert len(calls) == 2
    assert all("gemini-3.6-flash:" in path for path in calls)
    assert waits == [3]
    assert result["translated_count"] == 2


@pytest.mark.parametrize("retries", [0, 2, 5])
def test_persistent_overload_has_bounded_calls_and_keeps_resume_cache(
    tmp_path, monkeypatch, retries
):
    calls = []
    waits = []
    failing = [True]

    def handler(request):
        items = batch_of(request)
        calls.append([item["id"] for item in items])
        if calls[-1] == ["c2"] and failing[0]:
            return httpx.Response(503)
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": "translated"}
                        for item in items
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler, retries=retries)
    monkeypatch.setattr(
        threading.Event, "wait", lambda self, delay: waits.append(delay) or False
    )
    with pytest.raises(SubtitleTranslateError, match="503") as caught:
        translate_source_document(
            service,
            document(),
            model="gemini-3.6-flash",
            batch_size=1,
            cache_dir=tmp_path / "cache",
        )
    assert calls == [["c1"]] + [["c2"]] * (retries + 1)
    assert waits == [3] * retries
    assert caught.value.partial_result["translated_count"] == 1
    assert caught.value.partial_result["checkpointed_count"] == 1
    failing[0] = False
    resumed = translate_source_document(
        service,
        document(),
        model="gemini-3.6-flash",
        batch_size=1,
        cache_dir=tmp_path / "cache",
    )
    assert resumed["cache_hits"] == 1
    assert resumed["translated_count"] == 2
    assert calls.count(["c1"]) == 1


def test_fallback_recovery_shares_http_call_budget(tmp_path, monkeypatch):
    calls = []
    waits = []

    def handler(request):
        calls.append(request.url.path.split("/")[-1].split(":")[0])
        return httpx.Response(503)

    service = make_service(tmp_path, monkeypatch, handler, retries=4)
    monkeypatch.setattr(
        threading.Event, "wait", lambda self, delay: waits.append(delay) or False
    )
    with pytest.raises(SubtitleTranslateError, match="tổng số lần gọi"):
        translate_source_document(service, document(), model="gemini-3.8-flash")
    assert calls == [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
    ]
    assert waits == [3]


@pytest.mark.parametrize(
    "retry_after,expected_calls,expected_waits",
    [
        ("30", 2, [30]),
        ("900", 1, []),
    ],
)
def test_overload_respects_server_wait_and_batch_deadline(
    tmp_path, monkeypatch, retry_after, expected_calls, expected_waits
):
    calls = []
    waits = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": retry_after})

    service = make_service(tmp_path, monkeypatch, handler, retries=1)
    monkeypatch.setattr(
        threading.Event, "wait", lambda self, delay: waits.append(delay) or False
    )
    with pytest.raises(SubtitleTranslateError, match="503"):
        translate_source_document(service, document(), model="gemini-3.6-flash")
    assert len(calls) == expected_calls
    assert waits == expected_waits


def test_cancel_during_overload_wait_publishes_status_and_sends_no_more_requests(
    tmp_path, monkeypatch
):
    from app.services.subtitle_translate import SubtitleTranslateCanceled

    calls = []
    statuses = []

    class Context:
        cancel_event = threading.Event()

        def raise_if_canceled(self):
            assert not self.cancel_event.is_set()

        def update(self, *args):
            statuses.append(args)

    def cancel_wait(delay):
        assert delay == 3
        Context.cancel_event.set()
        return True

    service = make_service(
        tmp_path,
        monkeypatch,
        lambda request: calls.append(request) or httpx.Response(503),
        retries=2,
    )
    monkeypatch.setattr(Context.cancel_event, "wait", cancel_wait)
    with pytest.raises(SubtitleTranslateCanceled):
        translate_source_document(
            service, document(), model="gemini-3.6-flash", context=Context()
        )
    assert len(calls) == 1
    assert statuses[-1][1] == "retry_wait"
    assert "503" in statuses[-1][2]
    assert "3s" in statuses[-1][2]
    assert service.dispatcher._busy == {}


def test_unsupported_model_is_not_retried_as_overload(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    service = make_service(tmp_path, monkeypatch, handler, retries=5)
    with pytest.raises(SubtitleTranslateError, match="404"):
        translate_source_document(service, document(), model="gemini-3.6-flash")
    assert len(calls) == 1


def test_quota_is_reported_to_owning_key_before_releasing_lease(tmp_path, monkeypatch):
    keys = [
        {"id": "k1", "secret": "fixture-one", "enabled": True},
        {"id": "k2", "secret": "fixture-two", "enabled": True},
    ]
    calls = []

    def handler(request):
        calls.append(request.headers["x-goog-api-key"])
        if len(calls) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "60"},
                json={"error": {"message": "fixture quota"}},
            )
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": "translated"}
                        for item in batch_of(request)
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler, retries=2, keys=keys)
    original_report = service.dispatcher.report

    def report_while_owned(key, model, failure):
        assert key["id"] in service.dispatcher._busy
        original_report(key, model, failure)

    monkeypatch.setattr(service.dispatcher, "report", report_while_owned)
    result = translate_source_document(service, document())
    assert calls == ["fixture-one", "fixture-two"]
    assert result["translated_count"] == 2
    assert service.dispatcher.states()["k1"] == "quota_wait"


def test_invalid_id_batches_are_never_cached_or_partially_applied(
    tmp_path, monkeypatch
):
    def handler(request):
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": "c1", "translation": "one"},
                        {"id": "c1", "translation": "two"},
                        {"id": "c2", "translation": "three"},
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler, retries=1)
    with pytest.raises(SubtitleTranslateError) as caught:
        translate_source_document(service, document(), cache_dir=tmp_path / "cache")
    assert caught.value.partial_result["translated_count"] == 0
    assert not list((tmp_path / "cache").glob("*.json"))


def test_cancel_after_valid_response_keeps_checkpoint_for_new_service(
    tmp_path, monkeypatch
):
    from app.services.subtitle_jobs import SubtitleJobCanceled

    cancel = threading.Event()
    calls = []

    class Context:
        cancel_event = cancel

        def raise_if_canceled(self):
            if cancel.is_set():
                raise SubtitleJobCanceled("fixture canceled")

        def update(self, *args):
            self.raise_if_canceled()

        def update_details(self, *args):
            self.raise_if_canceled()

    def handler(request):
        items = batch_of(request)
        calls.append([item["id"] for item in items])
        if len(calls) == 1:
            cancel.set()
        return response(
            json.dumps(
                {
                    "translations": [
                        {
                            "id": item["id"],
                            "translation": "translated " + item["source_text"],
                        }
                        for item in items
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler)
    with pytest.raises(SubtitleJobCanceled):
        translate_source_document(
            service,
            document(),
            cache_dir=tmp_path / "cache",
            batch_size=1,
            context=Context(),
        )
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1
    cancel.clear()
    restarted = make_service(tmp_path, monkeypatch, handler)
    result = translate_source_document(
        restarted, document(), cache_dir=tmp_path / "cache", batch_size=1
    )
    assert result["cache_hits"] == 1
    assert calls == [["c1"], ["c2"]]


def test_all_request_timeout_phases_respect_remaining_deadline(tmp_path, monkeypatch):
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        return response("{}")

    service = make_service(tmp_path, monkeypatch, handler)
    service._generate_content_direct(
        "fixture", cancel_event=threading.Event(), deadline=time.monotonic() + 0.5
    )
    assert all(0 < value <= 0.5 for value in timeouts[0].values())


def test_source_edit_invalidates_affected_cache(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        items = batch_of(request)
        calls.append(items)
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": item["source_text"]}
                        for item in items
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler)
    original = document()
    translate_source_document(service, original, cache_dir=tmp_path / "cache")
    changed = original.model_copy(deep=True)
    changed.segments[0].text = "new corrected first"
    changed.revision += 1
    result = translate_source_document(service, changed, cache_dir=tmp_path / "cache")
    assert len(calls) == 2
    assert result["document"]["segments"][0]["text"] == "new corrected first"
    assert result["cache_hits"] == 0


def test_oversized_translation_is_rejected_before_checkpoint(tmp_path, monkeypatch):
    def handler(request):
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": "x" * 4001}
                        for item in batch_of(request)
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler)
    with pytest.raises(SubtitleTranslateError, match="4000") as caught:
        translate_source_document(service, document(), cache_dir=tmp_path / "cache")
    assert caught.value.partial_result["translated_count"] == 0
    assert not list((tmp_path / "cache").glob("*.json"))


def test_checkpoint_disk_error_preserves_valid_partial_and_prior_checkpoint(
    tmp_path, monkeypatch
):
    import errno

    from app.services import subtitle_translate

    calls = []

    def handler(request):
        items = batch_of(request)
        calls.append([item["id"] for item in items])
        return response(
            json.dumps(
                {
                    "translations": [
                        {
                            "id": item["id"],
                            "translation": "translated " + item["source_text"],
                        }
                        for item in items
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler)
    atomic = subtitle_translate.atomic_json
    writes = []

    def disk_full_on_second_batch(path, data):
        writes.append(path)
        if len(writes) == 2:
            raise OSError(errno.ENOSPC, "fixture disk full")
        return atomic(path, data)

    monkeypatch.setattr(subtitle_translate, "atomic_json", disk_full_on_second_batch)
    source = document()
    source.segments.append(
        SubtitleCueV2(id="c3", start_ms=2500, end_ms=3000, text="third")
    )
    with pytest.raises(SubtitleTranslateError, match="checkpoint") as caught:
        translate_source_document(
            service, source, batch_size=1, cache_dir=tmp_path / "cache"
        )
    partial = caught.value.partial_result
    assert partial["translated_count"] == 2
    assert partial["checkpointed_count"] == 1
    assert partial["untranslated_ids"] == ["c3"]
    assert calls == [["c1"], ["c2"]]  # Stop spending requests after persistence fails.
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1
    monkeypatch.setattr(subtitle_translate, "atomic_json", atomic)
    result = translate_source_document(
        service, source, batch_size=1, cache_dir=tmp_path / "cache"
    )
    assert result["checkpointed_count"] == 3
    assert calls == [["c1"], ["c2"], ["c2"], ["c3"]]


def test_http_client_creation_failure_restores_outer_deadline(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch, lambda request: response("{}"))
    outer = time.monotonic() + 300
    service._request_context.deadline = outer

    def unavailable_client():
        raise OSError("fixture client creation failure")

    monkeypatch.setattr(service, "_client", unavailable_client)
    with pytest.raises(OSError, match="client creation"):
        service._generate_content_direct(
            "fixture", cancel_event=threading.Event(), deadline=time.monotonic() + 1
        )
    assert service._request_context.deadline == outer


def test_cache_quota_does_not_evict_completed_batches_of_active_translation(
    tmp_path, monkeypatch
):
    from app.services import subtitle_cache

    monkeypatch.setattr(subtitle_cache, "MAX_ENTRIES", 1)
    calls = []

    def handler(request):
        items = batch_of(request)
        calls.append([item["id"] for item in items])
        return response(
            json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "translation": "translated"}
                        for item in items
                    ]
                }
            )
        )

    service = make_service(tmp_path, monkeypatch, handler)
    with pytest.raises(SubtitleTranslateError, match="checkpoint") as caught:
        translate_source_document(
            service, document(), batch_size=1, cache_dir=tmp_path / "cache"
        )
    assert caught.value.partial_result["checkpointed_count"] == 1
    assert caught.value.partial_result["translated_count"] == 2
    paths = list((tmp_path / "cache").glob("batch-*.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text())["translations"][0]["id"] == "c1"
    monkeypatch.setattr(subtitle_cache, "MAX_ENTRIES", 2)
    result = translate_source_document(
        service, document(), batch_size=1, cache_dir=tmp_path / "cache"
    )
    assert calls == [["c1"], ["c2"], ["c2"]]
    assert result["cache_hits"] == 1
