from app.services.insights import summarize_items


def item(
    item_id: int,
    source: str,
    language: str,
    sentiment: str,
    topics: list[tuple[str, str, list[str]]],
    reasons: list[str],
    trend: float,
) -> dict:
    return {
        "id": item_id,
        "title": f"Item {item_id}",
        "source_id": source,
        "canonical_url": f"https://example.test/{item_id}",
        "trend_score": trend,
        "insights": {
            "language": {"code": language, "label": language.upper()},
            "sentiment": {"label": sentiment, "reasons": reasons},
            "topics": [
                {"id": topic_id, "label": label, "reasons": signals}
                for topic_id, label, signals in topics
            ],
        },
    }


def test_summary_counts_multilabel_topics_and_deduplicates_item_signals() -> None:
    rows = [
        item(
            1,
            "web",
            "vi",
            "negative",
            [("bugs", "Bugs", ["crash"]), ("performance", "Performance", ["lag", "crash"])],
            ["negative: crash"],
            90,
        ),
        item(2, "steam", "en", "positive", [("gameplay", "Gameplay", ["combat"])], ["positive: fun"], 80),
        item(3, "web", "vi", "neutral", [], [], 70),
    ]

    summary = summarize_items(rows, top_limit=2)

    assert summary["total_items"] == 3
    assert summary["topic_coverage_count"] == 2
    assert summary["topic_coverage_percentage"] == 66.7
    assert summary["sources"][0] == {"id": "web", "label": "web", "count": 2, "percentage": 66.7}
    assert {row["id"]: row["count"] for row in summary["topics"]} == {
        "bugs": 1,
        "gameplay": 1,
        "performance": 1,
    }
    assert {row["id"]: row["count"] for row in summary["top_signals"]}["crash"] == 1
    assert [row["id"] for row in summary["top_items"]] == [1, 2]


def test_empty_summary_is_explicit_and_safe() -> None:
    summary = summarize_items([])
    assert summary["total_items"] == 0
    assert summary["topic_coverage_percentage"] == 0
    assert summary["sources"] == []
    assert summary["top_items"] == []
