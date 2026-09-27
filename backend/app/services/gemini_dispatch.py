"""Service-wide credential leases, scoped quota waits and per-run model fallback."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

FLASH_FALLBACK = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash")


@dataclass(frozen=True)
class ApiFailure:
    status: int
    operation: str
    category: str
    wait_seconds: float = 0
    quota_kind: str = "unknown"


def classify_failure(response: httpx.Response, operation: str, *, now: datetime | None = None) -> ApiFailure:
    now = now or datetime.now(UTC)
    code = response.status_code
    try:
        error = response.json().get("error", {})
        if not isinstance(error, dict):
            error = {}
    except (ValueError, AttributeError):
        error = {}
    message = str(error.get("message", "")).lower()
    details = error.get("details", [])
    details = details if isinstance(details, list) else []
    reasons = {str(d.get("reason", "")) for d in details if isinstance(d, dict)}
    if code == 429 or (code == 503 and operation == "generateContent"):
        wait = 3.0 if code == 429 else 0.0
        header = response.headers.get("Retry-After", "")
        try:
            wait = max(wait, float(header))
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                wait = max(wait, (date - now).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        quota_kind = "unknown"
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("@type", "")).endswith("RetryInfo"):
                delay = detail.get("retryDelay", "")
                try:
                    seconds = float(delay.get("seconds", 0)) + float(delay.get("nanos", 0)) / 1e9 if isinstance(delay, dict) else float(str(delay).removesuffix("s"))
                    wait = max(wait, seconds)
                except (TypeError, ValueError):
                    pass
            for violation in detail.get("violations", []):
                if not isinstance(violation, dict):
                    continue
                metric = str(violation.get("quotaId", "")) + str(violation.get("quotaMetric", ""))
                if "perday" in metric.lower() or "per_day" in metric.lower():
                    quota_kind = "daily"
                elif quota_kind != "daily" and ("perminute" in metric.lower() or "per_minute" in metric.lower()):
                    quota_kind = "minute"
        if code == 503:
            return ApiFailure(code, operation, "overloaded", wait)
        if quota_kind == "daily":
            # Never hammer a daily limit. Unknown reset timezone: conservative 24h cooldown.
            wait = max(wait, 86400)
        return ApiFailure(code, operation, "quota", wait, quota_kind)
    if code == 401 or "API_KEY_INVALID" in reasons or "API_KEY_EXPIRED" in reasons:
        return ApiFailure(code, operation, "authentication")
    if code == 403:
        return ApiFailure(code, operation, "permission")
    if (
        operation == "generateContent"
        and code in (400, 404)
        and "model" in message
        and any(s in message for s in (
            "not found", "does not exist", "not supported for generatecontent", "does not support"
        ))
    ):
        return ApiFailure(code, operation, "model_unavailable")
    return ApiFailure(code, operation, "transient" if code >= 500 else "request")


class DispatchUnavailable(RuntimeError):
    pass


class RequestBudgetExceeded(DispatchUnavailable):
    """Local request cap; retrying with a fresh budget must not bypass it."""


class NoEligibleKeys(DispatchUnavailable):
    """No enabled credential/model combination remains in this run."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ModelFallback:
    requested: str
    chain: tuple[str, ...] = FLASH_FALLBACK
    available: set[str] | None = None
    _blocked: set[tuple[str, str]] = field(default_factory=set, repr=False)
    _temporary: set[tuple[str, str]] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def candidates(self) -> tuple[str, ...]:
        chain = self.chain[self.chain.index(self.requested):] if self.requested in self.chain else (self.requested,)
        return tuple(model for model in chain if self.available is None or model in self.available or model == self.requested)

    def next_model(self, scope: str) -> str:
        with self._lock:
            for model in self.candidates():
                if (scope, model) not in self._blocked:
                    return model
        raise DispatchUnavailable("Đã hết chuỗi model khả dụng cho tác vụ này.")

    def block(self, scope: str, model: str, *, temporary: bool = False) -> None:
        with self._lock:
            if temporary and (scope, model) not in self._blocked:
                self._temporary.add((scope, model))
            elif not temporary:
                self._temporary.discard((scope, model))
            self._blocked.add((scope, model))

    def has_temporary_blocks(self, scope: str | None = None) -> bool:
        with self._lock:
            return any(scope is None or group == scope for group, _ in self._temporary)

    def reset_temporary(self) -> None:
        """Retry overloads after a cooldown; preserve unsupported-model decisions."""
        with self._lock:
            self._blocked.difference_update(self._temporary)
            self._temporary.clear()


class GeminiDispatcher:
    def __init__(self, provider: Callable[[], list[dict[str, Any]]], *, max_concurrent: int = 4,
                 group_concurrent: int = 1, compression_concurrent: int = 2):
        self.provider = provider
        # Legacy limits are accepted for old callers; enabled keys set capacity.
        del max_concurrent, group_concurrent
        self.compression = threading.BoundedSemaphore(max(1, compression_concurrent))
        self._condition = threading.Condition()
        self._busy: dict[str, tuple[str, str]] = {}
        self._cooldown: dict[tuple[str, str, str], float] = {}
        self._disabled: set[str] = set()
        self._permissions: set[tuple[str, str]] = set()
        self._lease_order: dict[str, int] = {}
        self._lease_sequence = 0

    @staticmethod
    def scope(key: dict[str, Any]) -> str:
        return f"key:{key['id']}"

    @contextmanager
    def lease(self, model: str, *, cancel_event: threading.Event, deadline: float, status=None, fallback: ModelFallback | None = None):
        chosen = None
        last_status = None
        while chosen is None:
            if cancel_event.is_set():
                raise DispatchUnavailable("Đã hủy điều phối Gemini")
            if time.monotonic() >= deadline:
                raise DispatchUnavailable("Hết thời gian chờ key/hạn mức Gemini.")
            # Fetch current snapshots on every scheduling pass: deleted/disabled keys stop receiving work.
            keys = self.provider()
            with self._condition:
                now = time.monotonic()
                eligible = []
                temporarily_blocked = False
                for key in keys:
                    if not key["enabled"] or key["id"] in self._disabled:
                        continue
                    try:
                        candidate_model = fallback.next_model(self.scope(key)) if fallback else model
                    except DispatchUnavailable:
                        temporarily_blocked |= fallback is not None and fallback.has_temporary_blocks(self.scope(key))
                        continue
                    if (key["id"], candidate_model) not in self._permissions:
                        eligible.append((key, candidate_model))
                if not eligible:
                    raise NoEligibleKeys("Không còn key/model khả dụng trong lượt chạy; đã dừng và giữ checkpoint.",
                                         retryable=temporarily_blocked)
                next_slots = [max((until for (group, limited_model, _), until in self._cooldown.items()
                                  if group == self.scope(key) and limited_model in (candidate_model, "*")), default=0)
                              for key, candidate_model in eligible]
                if min(next_slots) >= deadline:
                    raise NoEligibleKeys("Hạn mức của các key khả dụng còn chờ quá thời gian xử lý cho phép; đã dừng và giữ checkpoint.")
                # Prefer keys used least recently, including newly added keys.
                for key, candidate_model in sorted(eligible, key=lambda item: self._lease_order.get(item[0]["id"], 0)):
                    scope = self.scope(key)
                    waiting = max((until for (group, limited_model, _), until in self._cooldown.items()
                                   if group == scope and limited_model in (candidate_model, "*")), default=0)
                    if key["id"] not in self._busy and waiting <= now:
                        chosen = {**key, "leased_model": candidate_model}
                        self._busy[key["id"]] = (scope, candidate_model)
                        self._lease_sequence += 1
                        self._lease_order[key["id"]] = self._lease_sequence
                        break
                if chosen is None:
                    message = "Đang chờ key rảnh hoặc thời gian chờ hạn mức của key/model."
                    if status and message != last_status:
                        status(message)
                        last_status = message
                    self._condition.wait(timeout=min(0.25, max(0, deadline - now)))
        try:
            yield chosen
        finally:
            with self._condition:
                self._busy.pop(chosen["id"], None)
                self._condition.notify_all()

    def report(self, key: dict[str, Any], model: str, failure: ApiFailure) -> None:
        with self._condition:
            if failure.category == "authentication":
                self._disabled.add(key["id"])
            elif failure.category == "permission":
                self._permissions.add((key["id"], model))
            elif failure.category == "quota":
                # A response pauses only its owning key. File API is model-independent.
                limited_model = model if failure.operation == "generateContent" else "*"
                token = (self.scope(key), limited_model, failure.quota_kind)
                self._cooldown[token] = max(self._cooldown.get(token, 0), time.monotonic() + failure.wait_seconds)
            self._condition.notify_all()

    def blocked_failure(self, key: dict[str, Any], model: str) -> ApiFailure | None:
        """Recheck scoped cooldowns before an internal model fallback request."""
        with self._condition:
            if key["id"] in self._disabled:
                return ApiFailure(401, "generateContent", "authentication")
            if (key["id"], model) in self._permissions:
                return ApiFailure(403, "generateContent", "permission")
            now = time.monotonic()
            waits = [(until - now, kind) for (group, limited_model, kind), until in self._cooldown.items()
                     if group == self.scope(key) and limited_model in (model, "*") and until > now]
            if waits:
                remaining, kind = max(waits)
                return ApiFailure(429, "generateContent", "quota", remaining, kind)
        return None

    def states(self) -> dict[str, str]:
        with self._condition:
            now = time.monotonic()
            states = {}
            for key in self.provider():
                state = "disabled" if not key["enabled"] else "permission_error" if key["id"] in self._disabled else "busy" if key["id"] in self._busy else "quota_wait" if any(group == self.scope(key) and until > now for (group, _, _), until in self._cooldown.items()) else "untested"
                states[key["id"]] = state
            return states


class ChunkRequestBudget:
    """One deadline across File API, retries, model changes and credential changes."""
    def __init__(self, deadline: float, cancel_event: threading.Event, attempts: int):
        self.deadline = deadline
        self.cancel_event = cancel_event
        self.attempts = attempts
        self.generation_calls = 0
        self.upload_calls = 0

    def before_request(self, request: httpx.Request) -> None:
        if request.method == "DELETE":
            return  # Owner cleanup has its own short finite timeout even after cancel.
        remaining = self.deadline - time.monotonic()
        if self.cancel_event.is_set() or remaining <= 0:
            raise DispatchUnavailable("Đã hủy hoặc hết deadline xử lý đoạn Gemini.")
        if request.url.path.endswith(":generateContent"):
            self.generation_calls += 1
            if self.generation_calls > self.attempts:
                raise RequestBudgetExceeded("Đã hết tổng số lần gọi generateContent cho đoạn.")
        # Resumable upload uses two POSTs, often on the SAME URL path:
        # 'start' creates the session; 'upload, finalize' sends its bytes.
        # Only session creation consumes an upload attempt. Chunk/finalize/query
        # requests still obey the common cancellation and deadline checks above.
        commands = {part.strip().lower() for part in request.headers.get("X-Goog-Upload-Command", "").split(",")}
        if request.method == "POST" and request.url.path.rstrip("/") == "/upload/v1beta/files" and "start" in commands:
            self.upload_calls += 1
            if self.upload_calls > self.attempts:
                raise RequestBudgetExceeded("Đã hết tổng số lần khởi tạo upload cho đoạn.")
        timeouts = request.extensions.get("timeout", {})
        request.extensions["timeout"] = {key: min(value if value is not None else remaining, remaining) for key, value in timeouts.items()}
