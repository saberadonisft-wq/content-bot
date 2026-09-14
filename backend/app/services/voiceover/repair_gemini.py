"""Text-only Gemini repair requests with durable attempt accounting and cancellation."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import httpx

from ..gemini_dispatch import ModelFallback, classify_failure
from ..gemini_schema import response_schema
from ..gemini_subtitles import GeminiSubtitleError
from .repair_budget import RepairBudget


async def _post(client, url, payload, headers, budget, cancel):
    task = asyncio.create_task(client.post(url, json=payload, headers=headers))
    try:
        while not task.done():
            budget.check(cancel)
            await asyncio.wait({task}, timeout=.1)
        budget.check(cancel)
        return await task
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
            await task


def request_repair_json(service, budget: RepairBudget, cluster_ids: list[str], prompt: str, schema: dict,
                        *, cancel: threading.Event, transport=None, media_parts: list[dict] | None = None) -> str:
    model = service.resolve_model()
    fallback = ModelFallback(model)
    payload = {'contents': [{'parts': [{'text': prompt}, *(media_parts or [])]}], 'generationConfig': {
        'temperature': .1, 'responseMimeType': 'application/json', 'maxOutputTokens': 4096,
        'responseSchema': response_schema(schema)}}
    while True:
        budget.check(cancel)
        with service.dispatcher.lease(model, cancel_event=cancel,
                deadline=time.monotonic() + budget.remaining_seconds(), fallback=fallback) as key:
            chosen_model = key['leased_model']
            receipt = budget.reserve('gemini', cluster_ids, cancel=cancel)

            async def send(chosen_model=chosen_model, key=key):
                async with httpx.AsyncClient(base_url='https://generativelanguage.googleapis.com',
                        timeout=min(60, budget.remaining_seconds()), transport=transport) as client:
                    return await _post(client, f'/v1beta/models/{chosen_model}:generateContent', payload,
                        service._api_headers(key['secret']), budget, cancel)

            try:
                response = asyncio.run(send())
            except (httpx.TimeoutException, httpx.TransportError):
                budget.settle(receipt, succeeded=False, error='Gemini transport interrupted')
                if cancel.wait(min(2, budget.remaining_seconds())):
                    budget.check(cancel)
                continue
            except BaseException:
                budget.settle(receipt, succeeded=False, error='Request canceled or deadline reached')
                raise
            if response.status_code not in {200, 201}:
                failure = classify_failure(response, 'generateContent')
                budget.settle(receipt, succeeded=False, error=f'HTTP {failure.status}: {failure.category}')
                service.dispatcher.report(key, chosen_model, failure)
                if failure.category in {'overloaded', 'model_unavailable'}:
                    fallback.block(service.dispatcher.scope(key), chosen_model, temporary=failure.category == 'overloaded')
                if failure.category not in {'overloaded', 'transient', 'quota', 'model_unavailable'}:
                    raise GeminiSubtitleError(f'Gemini sửa giọng không thực hiện được: HTTP {failure.status}.')
                if cancel.wait(min(2, budget.remaining_seconds())):
                    budget.check(cancel)
                continue
            try:
                data = response.json()
                candidate = data.get('candidates', [{}])[0]
                if data.get('promptFeedback', {}).get('blockReason') or candidate.get('finishReason') != 'STOP':
                    raise ValueError('Gemini không trả kết quả sửa hoàn chỉnh hoặc đã chặn nội dung.')
                text = ''.join(part['text'] for part in candidate['content']['parts']
                    if not part.get('thought') and isinstance(part.get('text'), str))
                if not text.strip():
                    raise ValueError('Gemini không trả phương án sửa.')
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                budget.settle(receipt, succeeded=False, error='Incomplete or blocked Gemini response')
                raise GeminiSubtitleError('Không nhận được JSON sửa giọng hoàn chỉnh; giữ bản trước.') from exc
            budget.settle(receipt, succeeded=True)
            budget.check(cancel)
            return text
