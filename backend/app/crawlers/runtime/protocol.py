"""Strict newline-delimited JSON protocol shared by parent and crawler worker."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .contracts import _freeze
from .redaction import contains_secret, redact

PROTOCOL_VERSION = "cbce.worker.v1"
MAX_MESSAGE_BYTES = 1_048_576


class WorkerMessageKind(StrEnum):
    READY = "ready"
    START = "start"
    CANCEL = "cancel"
    AUTH_CONTINUE = "auth_continue"
    SHUTDOWN = "shutdown"
    RUN_STARTED = "run_started"
    BROWSER_OPENING = "browser_opening"
    AUTH_REQUIRED = "auth_required"
    AUTHENTICATED = "authenticated"
    CHALLENGE_REQUIRED = "challenge_required"
    PAGE_SCANNED = "page_scanned"
    ITEM = "item"
    COMMENT = "comment"
    MEDIA = "media"
    CHECKPOINT = "checkpoint"
    RATE_LIMITED = "rate_limited"
    HEARTBEAT = "heartbeat"
    WARNING = "warning"
    ERROR = "error"
    CANCELLED = "cancelled"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class WorkerEnvelope:
    kind: WorkerMessageKind
    sequence: int
    run_id: str | None
    source_run_id: str | None
    source_id: str | None
    provider_id: str | None
    operation: str | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    protocol_version: str = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("Worker message sequence cannot be negative")
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("Unsupported worker protocol version")
        if self.kind is not WorkerMessageKind.READY:
            required = (
                self.run_id,
                self.source_run_id,
                self.source_id,
                self.provider_id,
                self.operation,
            )
            if any(not str(value or "").strip() for value in required):
                raise ValueError("Bound worker messages require run/source/provider identity")
        if contains_secret(self.payload):
            raise ValueError("Worker message payload contains secret material")
        try:
            copied_payload = json.loads(
                json.dumps(self.payload, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Worker payload must be JSON-compatible") from exc
        object.__setattr__(self, "payload", _freeze(copied_payload))
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "kind": self.kind.value,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "source_run_id": self.source_run_id,
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "operation": self.operation,
            "timestamp": self.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "payload": _thaw(self.payload),
        }


def encode_message(message: WorkerEnvelope) -> bytes:
    encoded = (
        json.dumps(
            message.as_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("Worker message exceeds the protocol size limit")
    return encoded


def decode_message(line: bytes | str) -> WorkerEnvelope:
    raw = line.encode("utf-8") if isinstance(line, str) else line
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Worker message exceeds the protocol size limit")
    if b"\n" in raw.rstrip(b"\r\n"):
        raise ValueError("Expected exactly one NDJSON message")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Worker output is not valid JSON") from exc
    if not isinstance(data, dict):
        raise TypeError("Worker message must be a JSON object")
    try:
        timestamp = datetime.fromisoformat(str(data["timestamp"]))
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise TypeError("Worker payload must be an object")
        return WorkerEnvelope(
            protocol_version=str(data.get("protocol_version") or ""),
            kind=WorkerMessageKind(str(data["kind"])),
            sequence=int(data["sequence"]),
            run_id=_optional_text(data.get("run_id")),
            source_run_id=_optional_text(data.get("source_run_id")),
            source_id=_optional_text(data.get("source_id")),
            provider_id=_optional_text(data.get("provider_id")),
            operation=_optional_text(data.get("operation")),
            timestamp=timestamp,
            payload=payload,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Worker message failed schema validation") from exc


def safe_diagnostic(value: object, *, limit: int = 2_000) -> str:
    text = str(redact(str(value))).replace("\x00", "")
    return text[-limit:]


class SequenceTracker:
    def __init__(self) -> None:
        self._last_by_run: dict[str, int] = {}

    def accept(self, message: WorkerEnvelope) -> bool:
        if message.run_id is None:
            return message.kind is WorkerMessageKind.READY
        previous = self._last_by_run.get(message.run_id, -1)
        if message.sequence <= previous:
            return False
        self._last_by_run[message.run_id] = message.sequence
        return True


class BoundedMessageBuffer:
    def __init__(self, maxsize: int = 100) -> None:
        if maxsize <= 0:
            raise ValueError("Message buffer size must be positive")
        self._queue: asyncio.Queue[WorkerEnvelope] = asyncio.Queue(maxsize=maxsize)

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    async def put(self, message: WorkerEnvelope) -> None:
        await self._queue.put(message)

    async def get(self) -> WorkerEnvelope:
        return await self._queue.get()


def sanitized_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = redact(payload)
    assert isinstance(result, dict)
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [_thaw(item) for item in value]
    return value
