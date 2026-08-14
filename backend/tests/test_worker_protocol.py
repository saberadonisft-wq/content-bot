import asyncio

import pytest

from app.crawlers.runtime import (
    BoundedMessageBuffer,
    SequenceTracker,
    WorkerEnvelope,
    WorkerMessageKind,
    decode_message,
    encode_message,
    safe_diagnostic,
    sanitized_payload,
)
from app.crawlers.runtime.protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION
from app.crawlers.runtime.redaction import REDACTED, contains_secret


def message(sequence: int = 1, *, payload=None) -> WorkerEnvelope:
    return WorkerEnvelope(
        kind=WorkerMessageKind.ITEM,
        sequence=sequence,
        run_id="run-1",
        source_run_id="source-run-1",
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="search",
        payload=payload or {"external_id": "BV1", "title": "Public post"},
    )


def test_worker_message_round_trip_is_single_line_and_versioned() -> None:
    encoded = encode_message(message())
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    decoded = decode_message(encoded)
    assert decoded.protocol_version == PROTOCOL_VERSION
    assert decoded.kind is WorkerMessageKind.ITEM
    assert decoded.payload["external_id"] == "BV1"


def test_worker_payload_is_defensively_frozen_after_secret_check() -> None:
    payload = {"record": {"title": "safe"}}
    envelope = message(payload=payload)
    payload["record"]["cookie"] = "late-secret"

    assert "cookie" not in envelope.payload["record"]
    with pytest.raises(TypeError):
        envelope.payload["record"]["title"] = "changed"


@pytest.mark.parametrize(
    "payload",
    [
        {"cookie": "session=secret"},
        {"headers": {"Authorization": "Bearer abcdefghijklmnop"}},
        {"url": "https://example.test/api?access_token=secret"},
        {"qr_data": "data:image/png;base64,abcdef"},
    ],
)
def test_protocol_rejects_secret_bearing_payloads(payload) -> None:
    with pytest.raises(ValueError, match="secret"):
        message(payload=payload)


def test_secret_references_are_allowed_but_values_are_redacted() -> None:
    envelope = message(payload={"credential_ref": "vault://x-production"})
    assert envelope.payload["credential_ref"] == "vault://x-production"
    sanitized = sanitized_payload(
        {
            "authorization": "Bearer abcdefghijklmnop",
            "nested": {"refresh_token": "secret", "safe": "value"},
        }
    )
    assert sanitized == {
        "authorization": REDACTED,
        "nested": {"refresh_token": REDACTED, "safe": "value"},
    }
    assert contains_secret(sanitized) is False


def test_diagnostic_redacts_headers_bearer_query_tokens_qr_and_userinfo() -> None:
    diagnostic = safe_diagnostic(
        "Authorization: Bearer abcdefghijklmnop\n"
        "GET https://user:pass@example.test/api?access_token=secret&ok=1 "
        "data:image/png;base64,abcdef"
    )
    assert "abcdefghijklmnop" not in diagnostic
    assert "secret" not in diagnostic
    assert "user:pass" not in diagnostic
    assert "base64" not in diagnostic
    assert diagnostic.count(REDACTED) >= 3


def test_decoder_rejects_multiline_oversized_and_unbound_messages() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        decode_message(encode_message(message()) + encode_message(message(2)))
    with pytest.raises(ValueError, match="size"):
        decode_message(b"x" * (MAX_MESSAGE_BYTES + 1))
    with pytest.raises(ValueError, match="identity"):
        WorkerEnvelope(
            kind=WorkerMessageKind.COMPLETE,
            sequence=1,
            run_id=None,
            source_run_id=None,
            source_id=None,
            provider_id=None,
            operation=None,
        )


def test_sequence_tracker_discards_duplicates_per_run() -> None:
    tracker = SequenceTracker()
    assert tracker.accept(message(1)) is True
    assert tracker.accept(message(1)) is False
    assert tracker.accept(message(0)) is False
    assert tracker.accept(message(3)) is True


def test_bounded_message_buffer_applies_backpressure() -> None:
    async def run() -> list[int]:
        buffer = BoundedMessageBuffer(maxsize=1)
        await buffer.put(message(1))
        blocked = asyncio.create_task(buffer.put(message(2)))
        await asyncio.sleep(0)
        assert blocked.done() is False
        first = await buffer.get()
        await blocked
        second = await buffer.get()
        return [first.sequence, second.sequence]

    assert asyncio.run(run()) == [1, 2]
