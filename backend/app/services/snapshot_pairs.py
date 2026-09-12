"""Keep the latest two observations per item without retaining its full history."""

from collections.abc import Iterable
from typing import Any


def latest_pairs(rows: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    pairs: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        pair = pairs.setdefault(row["content_item_id"], [])
        pair.append(row)
        pair.sort(
            key=lambda item: (item.get("captured_at") is None, item.get("captured_at")),
            reverse=True,
        )
        del pair[2:]
    return [pair for pair in pairs.values() if len(pair) == 2]
