from app.services.run_planner import plan_run_targets

CHANNELS = [
    {"id": "bsky-one", "source_id": "bluesky", "url": "https://bsky.app/profile/a", "mode": "public", "enabled": True},
    {"id": "x-view", "source_id": "x", "url": "https://x.com/a", "mode": "embed_only", "enabled": True},
    {"id": "disabled", "source_id": "reddit", "url": "https://reddit.com/r/a", "mode": "api", "enabled": False},
]


def test_planner_unions_global_and_channel_targets_in_stable_order() -> None:
    targets = plan_run_targets(
        ["youtube", "bluesky"], CHANNELS, {"youtube", "bluesky", "x", "reddit"}
    )
    assert [(row["source_id"], row.get("channel_id")) for row in targets] == [
        ("youtube", None),
        ("bluesky", None),
        ("bluesky", "bsky-one"),
    ]


def test_explicit_selectors_are_independent_and_empty_means_none() -> None:
    assert plan_run_targets(
        ["youtube"], CHANNELS, {"youtube", "bluesky", "x"}, source_ids=[], channel_ids=["bsky-one"]
    ) == [
        {"source_id": "bluesky", "channel_id": "bsky-one", "channel_url": "https://bsky.app/profile/a", "channel_label": "https://bsky.app/profile/a"}
    ]
    assert plan_run_targets(
        ["youtube"], CHANNELS, {"youtube", "bluesky", "x"}, source_ids=["youtube"], channel_ids=[]
    ) == [{"source_id": "youtube"}]


def test_capability_resolver_can_disable_planned_or_unsupported_targets() -> None:
    targets = plan_run_targets(
        ["youtube", "planned"],
        CHANNELS,
        {"youtube", "planned", "bluesky", "x"},
        capability_resolver=lambda source, operation, channel: not (
            source in {"planned", "x"} or (source == "bluesky" and operation == "scan_channel")
        ),
    )
    assert targets == [{"source_id": "youtube"}]
