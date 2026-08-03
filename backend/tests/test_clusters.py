from app.services.clusters import cluster_items


def item(
    item_id: int,
    title: str,
    source_id: str,
    canonical_url: str,
    body: str = "",
    trend_score: float = 50,
) -> dict:
    return {
        "id": item_id,
        "title": title,
        "body_snippet": body,
        "source_id": source_id,
        "canonical_url": canonical_url,
        "trend_score": trend_score,
        "published_at": f"2026-08-{item_id:02d}T08:00:00",
        "insights": {
            "language": {"code": "en"},
            "sentiment": {"label": "neutral"},
            "topics": [{"label": "Update"}],
        },
    }


def test_clusters_strong_titles_across_distinct_web_publishers() -> None:
    result = cluster_items(
        [
            item(1, "Major expansion release date revealed with gameplay details", "web", "https://one.test/a", trend_score=80),
            item(2, "Gameplay details reveal major expansion release date", "web", "https://two.test/b", trend_score=70),
        ],
        ["Example Game"],
    )

    assert result["cluster_count"] == 1
    cluster = result["clusters"][0]
    assert cluster["item_count"] == 2
    assert cluster["origin_count"] == 2
    assert cluster["item_hosts"] == ["one.test", "two.test"]
    assert cluster["match_reasons"][0].startswith("shared title terms:")


def test_does_not_cluster_generic_same_source_reviews() -> None:
    result = cluster_items(
        [
            item(1, "Recommended — Hades II", "steam", "https://steamcommunity.com/a"),
            item(2, "Recommended — Hades II", "steam", "https://steamcommunity.com/b"),
        ],
        ["Hades II"],
    )

    assert result["cluster_count"] == 0
    assert result["clustered_items"] == 0


def test_shared_outbound_link_can_cluster_same_source_posts() -> None:
    shared = "https://game.test/news/reveal-details?utm_source=social"
    result = cluster_items(
        [
            item(1, "Watch this", "bluesky", "https://bsky.app/a", f"News: {shared}"),
            item(2, "New details", "bluesky", "https://bsky.app/b", "https://game.test/news/reveal-details?fbclid=123"),
        ],
        ["Example Game"],
    )

    assert result["cluster_count"] == 1
    assert result["clusters"][0]["match_reasons"] == ["shared specific link: game.test"]


def test_generic_shared_homepage_is_not_story_evidence() -> None:
    result = cluster_items(
        [
            item(1, "Performance patch fixes severe frame drops today", "bluesky", "https://bsky.app/a", "https://game.test/"),
            item(2, "Paid cosmetic bundle arrives in the item shop", "bluesky", "https://bsky.app/b", "https://game.test/"),
        ],
        ["Example Game"],
    )

    assert result["cluster_count"] == 0


def test_clusters_transitive_specific_link_evidence() -> None:
    result = cluster_items(
        [
            item(1, "First post", "bluesky", "https://bsky.app/a", "https://game.test/news/expansion-reveal"),
            item(2, "Bridge post", "web", "https://one.test/b", "https://game.test/news/expansion-reveal https://game.test/news/expansion-details"),
            item(3, "Third post", "web", "https://two.test/c", "https://game.test/news/expansion-details"),
        ],
        ["Example Game"],
    )

    assert result["cluster_count"] == 1
    assert result["clusters"][0]["item_count"] == 3


def test_game_name_alone_is_not_a_cluster_signal() -> None:
    result = cluster_items(
        [
            item(1, "Black Myth Wukong review and impressions", "web", "https://one.test/a"),
            item(2, "Black Myth Wukong official trailer video", "bluesky", "https://bsky.app/b"),
        ],
        ["Black Myth Wukong"],
    )

    assert result["cluster_count"] == 0


def test_clusters_are_deterministic_and_respect_limit() -> None:
    items = [
        item(1, "Expansion launch date reveals combat system details", "web", "https://one.test/a", trend_score=90),
        item(2, "Combat system details reveal expansion launch date", "web", "https://two.test/b", trend_score=80),
        item(3, "Studio roadmap confirms multiplayer update schedule", "web", "https://three.test/c", trend_score=70),
        item(4, "Multiplayer update schedule confirmed in studio roadmap", "web", "https://four.test/d", trend_score=60),
    ]

    first = cluster_items(items, ["Example Game"], limit=1)
    second = cluster_items(list(reversed(items)), ["Example Game"], limit=1)

    assert first == second
    assert first["cluster_count"] == 2
    assert first["clustered_items"] == 4
    assert first["returned_cluster_count"] == 1
    assert first["returned_clustered_items"] == 2
    assert first["truncated"] is True
