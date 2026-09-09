"""Package canonical ASS layout with millisecond Matroska packet timing."""

from __future__ import annotations

import struct
from typing import Any

from .subtitles import cue_times_ms, subtitles_to_ass


def _element(identifier: int, payload: bytes) -> bytes:
    tag = identifier.to_bytes((identifier.bit_length() + 7) // 8, "big")
    for width in range(1, 9):
        if len(payload) < (1 << (7 * width)) - 1:
            size = ((1 << (7 * width)) | len(payload)).to_bytes(width, "big")
            return tag + size + payload
    raise ValueError("Subtitle track is too large")


def _uint(identifier: int, value: int) -> bytes:
    return _element(
        identifier, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    )


def subtitles_to_matroska(
    cues: list[dict[str, Any]],
    options: dict[str, Any],
    *,
    play_res_x: int,
    play_res_y: int,
) -> bytes:
    # ASS text timestamps only store centiseconds. Matroska stores the same
    # styles/events with independent millisecond packet start and duration.
    header = subtitles_to_ass([], options, play_res_x=play_res_x, play_res_y=play_res_y)
    ebml = _element(
        0x1A45DFA3,
        b"".join(
            [
                _uint(0x4286, 1),
                _uint(0x42F7, 1),
                _uint(0x42F2, 4),
                _uint(0x42F3, 8),
                _element(0x4282, b"matroska"),
                _uint(0x4287, 4),
                _uint(0x4285, 2),
            ]
        ),
    )
    duration = max((cue_times_ms(cue)[1] for cue in cues), default=0)
    info = _element(
        0x1549A966,
        b"".join(
            [
                _uint(0x2AD7B1, 1_000_000),
                _element(0x4489, struct.pack(">d", duration)),
                _element(0x4D80, b"content-bot"),
                _element(0x5741, b"content-bot"),
            ]
        ),
    )
    tracks = _element(
        0x1654AE6B,
        _element(
            0xAE,
            b"".join(
                [
                    _uint(0xD7, 1),
                    _uint(0x73C5, 1),
                    _uint(0x83, 17),
                    _uint(0x9C, 0),
                    _element(0x86, b"S_TEXT/ASS"),
                    _element(0x63A2, header.encode("utf-8")),
                ]
            ),
        ),
    )
    clusters: list[bytes] = []
    read_order = 0
    for cue in sorted(cues, key=lambda item: cue_times_ms(item)[0]):
        start_ms, end_ms = cue_times_ms(cue)
        document = subtitles_to_ass(
            [cue],
            options,
            play_res_x=play_res_x,
            play_res_y=play_res_y,
        )
        blocks: list[bytes] = []
        for line in document.splitlines():
            if not line.startswith("Dialogue: "):
                continue
            fields = line.removeprefix("Dialogue: ").split(",", 9)
            packet = f"{read_order},{fields[0]},{','.join(fields[3:])}".encode()
            blocks.append(
                _element(
                    0xA0,
                    _element(0xA1, b"\x81\x00\x00\x00" + packet)
                    + _uint(0x9B, end_ms - start_ms),
                )
            )
            read_order += 1
        if blocks:
            clusters.append(
                _element(0x1F43B675, _uint(0xE7, start_ms) + b"".join(blocks))
            )
    return ebml + _element(0x18538067, info + tracks + b"".join(clusters))
