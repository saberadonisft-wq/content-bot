from __future__ import annotations

from collections import Counter
from typing import Any


def _buckets(
    counts: Counter[str],
    total: int,
    labels: dict[str, str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = sorted(counts.items(), key=lambda row: (-row[1], row[0]))
    if limit is not None:
        rows = rows[:limit]
    return [
        {
            "id": key,
            "label": (labels or {}).get(key, key),
            "count": count,
            "percentage": round(100 * count / total, 1) if total else 0.0,
        }
        for key, count in rows
    ]


def summarize_items(items: list[dict[str, Any]], top_limit: int = 5) -> dict[str, Any]:
    """Aggregate already-filtered item payloads; topic percentages are multi-label."""

    total = len(items)
    sources: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    sentiments: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    signals: Counter[str] = Counter()
    language_labels: dict[str, str] = {}
    topic_labels: dict[str, str] = {}
    topic_coverage = 0

    for item in items:
        insight = item["insights"]
        sources[item["source_id"]] += 1
        language = insight["language"]
        languages[language["code"]] += 1
        language_labels[language["code"]] = language["label"]
        sentiments[insight["sentiment"]["label"]] += 1
        if insight["topics"]:
            topic_coverage += 1

        item_signals = {
            reason.split(": ", 1)[-1]
            for reason in insight["sentiment"]["reasons"]
            if reason
        }
        for topic in insight["topics"]:
            topics[topic["id"]] += 1
            topic_labels[topic["id"]] = topic["label"]
            item_signals.update(reason for reason in topic["reasons"] if reason)
        signals.update(item_signals)

    top_items = [
        {
            "id": item["id"],
            "title": item["title"],
            "source_id": item["source_id"],
            "canonical_url": item["canonical_url"],
            "trend_score": item["trend_score"],
            "language": item["insights"]["language"]["code"],
            "sentiment": item["insights"]["sentiment"]["label"],
            "topics": [topic["label"] for topic in item["insights"]["topics"]],
        }
        for item in items[:top_limit]
    ]
    return {
        "total_items": total,
        "topic_coverage_count": topic_coverage,
        "topic_coverage_percentage": round(100 * topic_coverage / total, 1) if total else 0.0,
        "sources": _buckets(sources, total),
        "languages": _buckets(languages, total, language_labels),
        "sentiments": _buckets(sentiments, total),
        "topics": _buckets(topics, total, topic_labels),
        "top_signals": _buckets(signals, total, limit=10),
        "top_items": top_items,
        "method": "rule-based-v1",
        "caveat": "Percentages describe the filtered public-item sample. Topics can overlap, and heuristic labels are not ground truth.",
    }
