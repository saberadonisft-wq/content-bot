import asyncio
import json
import threading
import time

import httpx
import pytest

from app.services.gemini_subtitles import (
    GeminiSubtitleError,
    GeminiSubtitleService,
    GeminiSubtitleSettings,
)
from app.services.voiceover.repair_budget import RepairBudget, RepairBudgetExceeded
from app.services.voiceover.repair_gemini import request_repair_json
from app.services.voiceover.repair_text import RepairResponse


class NoDelay(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


def setup(tmp_path):
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path), api_key_provider=lambda: 'fake-test-key')
    budget = RepairBudget(tmp_path / 'budget.json', input_binding='a' * 64, cluster_ids=['one'])
    return service, budget


def test_every_http_retry_is_reserved_and_hard_limited(tmp_path):
    service, budget = setup(tmp_path)
    requests = []

    def unavailable(request):
        requests.append(request)
        assert budget.snapshot()['gemini_used'] == len(requests)  # Durable BEFORE actual dispatch.
        raise httpx.ConnectError('fixture connection error')

    with pytest.raises(RepairBudgetExceeded):
        request_repair_json(service, budget, ['one'], 'fixture', RepairResponse.model_json_schema(),
            cancel=NoDelay(), transport=httpx.MockTransport(unavailable))
    assert len(requests) == 8
    assert all(receipt['state'] == 'failed' for receipt in budget.snapshot()['receipts'].values())
    assert budget.snapshot()['tts_used'] == 0


def test_text_request_has_no_video_upload_and_ignores_thought_parts(tmp_path):
    service, budget = setup(tmp_path)

    def answer(request):
        payload = json.loads(request.content)
        assert payload['contents'][0]['parts'] == [{'text': 'fixture'}]
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'thought': True, 'text': 'internal fixture'}, {'text': '{"rows":[]}'}]}}]})

    output = request_repair_json(service, budget, ['one'], 'fixture', RepairResponse.model_json_schema(),
        cancel=threading.Event(), transport=httpx.MockTransport(answer))
    assert output == '{"rows":[]}' and budget.snapshot()['gemini_used'] == 1


def test_cancel_aborts_pending_network_and_charges_the_inflight_attempt(tmp_path):
    service, budget = setup(tmp_path)
    cancel, stopped = threading.Event(), threading.Event()

    async def delayed(_request):
        cancel.set()
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    started = time.monotonic()
    with pytest.raises(InterruptedError):
        request_repair_json(service, budget, ['one'], 'fixture', RepairResponse.model_json_schema(),
            cancel=cancel, transport=httpx.MockTransport(delayed))
    assert stopped.is_set() and time.monotonic() - started < 2
    assert budget.snapshot()['gemini_used'] == 1


def test_blocked_content_is_not_retried_on_other_keys_or_models(tmp_path):
    service, budget = setup(tmp_path)
    calls = []

    def blocked(request):
        calls.append(request)
        return httpx.Response(200, json={'promptFeedback': {'blockReason': 'SAFETY'}})

    with pytest.raises(GeminiSubtitleError):
        request_repair_json(service, budget, ['one'], 'fixture', RepairResponse.model_json_schema(),
            cancel=NoDelay(), transport=httpx.MockTransport(blocked))
    assert len(calls) == 1 and budget.snapshot()['gemini_used'] == 1
