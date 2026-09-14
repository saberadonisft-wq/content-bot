import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest

from app.services.gemini_dispatch import (
    ApiFailure,
    DispatchUnavailable,
    GeminiDispatcher,
    ModelFallback,
    classify_failure,
)
from app.services.gemini_subtitles import (
    GeminiApiError,
    GeminiSubtitleService,
    GeminiSubtitleSettings,
)


def key(i, group=None):
    return {"id": str(i), "name": f"key {i}", "enabled": True, "secret": f"secret-{i}", "project_group": group}


@pytest.mark.parametrize("group", [None, "same-legacy-group"])
def test_all_twelve_keys_run_concurrently_without_group_limits(group):
    keys = [key(i, group) for i in range(12)]
    dispatcher = GeminiDispatcher(lambda: keys, max_concurrent=4, group_concurrent=1)
    barrier = threading.Barrier(12, timeout=5)
    lock = threading.Lock()
    active = {}
    peak = [0]
    completed = []
    def run(i):
        with dispatcher.lease("model", cancel_event=threading.Event(), deadline=time.monotonic() + 5) as selected:
            with lock:
                assert selected["id"] not in active
                active[selected["id"]] = selected["project_group"]
                peak[0] = max(peak[0], len(active))
                assert peak[0] <= 12
            barrier.wait()
            time.sleep(0.04 if i % 2 == 0 else 0.01)
            with lock:
                del active[selected["id"]]
                completed.append(i)
        return i
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert list(pool.map(run, range(12))) == list(range(12))
    assert peak[0] == 12
    assert sorted(completed) == list(range(12))
    assert completed != sorted(completed)


def test_disabled_inflight_key_finishes_but_receives_no_new_work():
    rows = [key(1, "a"), key(2, "b")]
    dispatcher = GeminiDispatcher(lambda: rows)
    with dispatcher.lease("m", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as owned:
        assert owned["id"] == "1"
        rows[0] = {**rows[0], "enabled": False}
        assert owned["secret"] == "secret-1"
    with dispatcher.lease("m", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as selected:
        assert selected["id"] == "2"


def test_quota_scopes_and_permission_are_model_specific():
    rows = [key(1, "a"), key(2, "a"), key(3, "b")]
    dispatcher = GeminiDispatcher(lambda: rows)
    dispatcher.report(rows[0], "m", ApiFailure(429, "generateContent", "quota", 86400, "daily"))
    with dispatcher.lease("m", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as selected:
        assert selected["id"] == "2"
    dispatcher.report(rows[2], "m", ApiFailure(403, "generateContent", "permission"))
    with dispatcher.lease("other", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as selected:
        assert selected["id"] == "1"
    assert rows[0]["project_group"] == "a"


def test_quota_on_one_unassigned_key_does_not_pause_other_keys():
    rows = [key(1), key(2)]
    dispatcher = GeminiDispatcher(lambda: rows)
    dispatcher.report(rows[0], "m", ApiFailure(429, "generateContent", "quota", 60))
    with dispatcher.lease("m", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as selected:
        assert selected["id"] == "2"
    assert dispatcher.blocked_failure(rows[0], "m") is not None
    assert dispatcher.blocked_failure(rows[1], "m") is None
    assert all(row["project_group"] is None for row in rows)


def test_key_selection_rotates_and_never_leases_one_key_twice():
    rows = [key(i) for i in range(12)]
    dispatcher = GeminiDispatcher(lambda: rows)
    used = []
    for _ in range(24):
        with dispatcher.lease("m", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as selected:
            used.append(selected["id"])
    assert used == [str(i) for i in range(12)] * 2
    with (
        dispatcher.lease("m", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as first,
        dispatcher.lease("m", cancel_event=threading.Event(), deadline=time.monotonic() + 1) as second,
    ):
        assert first["id"] != second["id"]


def test_daily_quota_and_retry_after_date_and_model_error_classification():
    response = httpx.Response(429, headers={"Retry-After": "Wed, 01 Jan 2025 00:10:00 GMT"}, json={"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDay"}]}]}})
    failure = classify_failure(response, "generateContent", now=datetime(2025, 1, 1, tzinfo=UTC))
    assert failure.quota_kind == "daily" and failure.wait_seconds >= 86400
    assert classify_failure(httpx.Response(503), "poll file state").category == "transient"
    assert classify_failure(httpx.Response(404, json={"error": {"message": "file not found"}}), "generateContent").category == "request"
    assert classify_failure(httpx.Response(404, json={"error": {"message": "models/abc not found"}}), "generateContent").category == "model_unavailable"


@pytest.mark.parametrize("requested,expected", [("gemini-3.8-flash", ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]), ("gemini-3.7-flash", ["gemini-3.7-flash", "gemini-3.6-flash"]), ("custom-model", ["custom-model"])])
def test_503_fallback_is_immediate_and_never_upgrades(tmp_path, monkeypatch, requested, expected):
    calls = []
    def send(request):
        calls.append(request.url.path.split("/")[-1].split(":")[0])
        return httpx.Response(503)
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path, api_key="fake"))
    monkeypatch.setattr(service, "_wait_for_retry", lambda *a, **k: pytest.fail("503 must not sleep"))
    fallback = ModelFallback(requested)
    with httpx.Client(transport=httpx.MockTransport(send), base_url="https://example.test") as client, pytest.raises(GeminiApiError):
        service._generate_content("files/a", "prompt", client=client, cancel_event=threading.Event(), model=requested, fallback=fallback)
    assert calls == expected
    with pytest.raises(DispatchUnavailable):
        fallback.next_model("unknown-project")
    assert ModelFallback(requested).next_model("unknown-project") == requested


def test_recovery_resets_only_overloaded_models_and_keeps_auth_and_daily_quota():
    fallback = ModelFallback("gemini-3.8-flash")
    rows = [key(1)]
    dispatcher = GeminiDispatcher(lambda: rows)
    scope = dispatcher.scope(rows[0])
    fallback.block(scope, "gemini-3.8-flash")  # Model not found is permanent for this run.
    fallback.block(scope, "gemini-3.7-flash", temporary=True)
    fallback.block(scope, "gemini-3.6-flash", temporary=True)
    from app.services.gemini_dispatch import NoEligibleKeys
    with pytest.raises(NoEligibleKeys) as error, dispatcher.lease(
        "gemini-3.8-flash", fallback=fallback, cancel_event=threading.Event(), deadline=time.monotonic() + 1):
        pytest.fail("All fallback models are blocked")
    assert error.value.retryable
    fallback.reset_temporary()
    assert fallback.next_model(scope) == "gemini-3.7-flash"
    dispatcher.report(rows[0], "gemini-3.7-flash", ApiFailure(429, "generateContent", "quota", 86400, "daily"))
    fallback.reset_temporary()
    with pytest.raises(NoEligibleKeys) as error, dispatcher.lease(
        "gemini-3.8-flash", fallback=fallback, cancel_event=threading.Event(), deadline=time.monotonic() + 1):
        pytest.fail("Daily quota must remain blocked")
    assert not error.value.retryable
    dispatcher.report(rows[0], "gemini-3.7-flash", ApiFailure(401, "generateContent", "authentication"))
    fallback.block(scope, "gemini-3.7-flash", temporary=True)
    fallback.block(scope, "gemini-3.6-flash", temporary=True)
    with pytest.raises(NoEligibleKeys) as error, dispatcher.lease(
        "gemini-3.8-flash", fallback=fallback, cancel_event=threading.Event(), deadline=time.monotonic() + 1):
        pytest.fail("Invalid credentials must remain disabled")
    assert not error.value.retryable
