"""Shared filtering contract and versioned, content-addressed item analysis."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from ..schemas import ItemOutput
from .text import content_insights, insights_match

ANALYSIS_VERSION = "rule-based-v1:1"


@dataclass(frozen=True)
class ItemQuery:
    keyword_id: int
    source_id: str | None = None
    query: str | None = None
    language: str | None = None
    sentiment: str | None = None
    topic: str | None = None
    min_relevance: float = 0
    session_id: str | None = None

    @property
    def needle(self) -> str:
        return (self.query or "").strip().casefold()


def analysis_input(item: dict) -> str:
    return json.dumps([
        item.get("title", ""), item.get("body_snippet", ""),
        item.get("hashtags", []), item.get("locale"),
    ], ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=2048)
def _analyze(encoded: str, version: str) -> str:
    del version  # Part of the cache key; bump when heuristic behavior changes.
    title, body, hashtags, locale = json.loads(encoded)
    return json.dumps(content_insights(title, body, hashtags, locale), ensure_ascii=False)


def analyze_item(item: dict) -> dict[str, Any]:
    # Return an independent value: response consumers must not mutate cached data.
    return json.loads(_analyze(analysis_input(item), ANALYSIS_VERSION))


def analysis_fingerprint(item: dict) -> str:
    return hashlib.sha256(analysis_input(item).encode()).hexdigest()


def search_text(item: dict) -> str:
    return " ".join(item.get(field, "") for field in ("title", "body_snippet", "author")).casefold()


def sort_timestamp(item: dict) -> str:
    value = item.get("published_at") or item.get("first_seen_at")
    if value is None:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def item_output(item: dict, match: dict, analysis: dict | None = None) -> ItemOutput:
    return ItemOutput(
        id=item["id"], source_id=item["source_id"], canonical_url=item["canonical_url"],
        title=item.get("title", ""), body_snippet=item.get("body_snippet", ""),
        author=item.get("author", ""), hashtags=item.get("hashtags", []),
        locale=item.get("locale"), published_at=item.get("published_at"),
        metrics=item.get("metrics", {}), relevance_score=match.get("relevance_score", 0),
        trend_score=match.get("trend_score", 0), match_reasons=match.get("match_reasons", []),
        insights=analysis if analysis is not None else analyze_item(item),
        first_seen_at=item["first_seen_at"], last_seen_at=item["last_seen_at"],
        caption_original=item.get("caption_original"),
        caption_edited=item.get("caption_edited"),
        caption_edited_at=item.get("caption_edited_at"),
    )


def query_items(store, query: ItemQuery, *, limit: int | None = None, offset: int = 0, indexed: bool = True):
    """Return (total, outputs); all filters apply before pagination."""
    if indexed and hasattr(store, "query_items"):
        total, rows = store.query_items(query, limit=limit, offset=offset)
        return total, [item_output(item, match, analysis) for item, match, analysis in rows]
    rows = store.item_matches(
        query.keyword_id, positive_only=True, source_id=query.source_id or None,
        min_relevance=query.min_relevance, session_id=query.session_id or None,
    )
    selected = []
    for item, match in rows:
        if query.needle and query.needle not in search_text(item):
            continue
        analysis = analyze_item(item)
        if insights_match(analysis, query.language, query.sentiment, query.topic):
            selected.append((item, match, analysis))
    selected.sort(key=lambda row: (row[1].get("trend_score", 0), sort_timestamp(row[0]), -row[0]["id"]), reverse=True)
    total = len(selected)
    selected = selected[offset:] if limit is None else selected[offset:offset + limit]
    return total, [item_output(item, match, deepcopy(analysis)) for item, match, analysis in selected]
