"""Pure run-target planning shared by manual and scheduled crawls."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

CapabilityResolver = Callable[[str, str, dict[str, Any] | None], bool | None]


def plan_run_targets(
    configured_source_ids: Sequence[str],
    channels: Sequence[dict[str, Any]],
    available_source_ids: Iterable[str],
    *,
    source_ids: Sequence[str] | None = None,
    channel_ids: Sequence[str] | None = None,
    capability_resolver: CapabilityResolver | None = None,
) -> list[dict[str, Any]]:
    """Return a stable union of global and saved-channel targets.

    ``None`` means use the topic configuration while an explicit empty list
    selects no targets of that kind. Global source and channel selectors are
    independent so a topic can run both for the same platform.
    """

    available = set(available_source_ids)
    selected_sources = configured_source_ids if source_ids is None else source_ids
    selected_channel_ids = None if channel_ids is None else set(channel_ids)
    targets: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_channels: set[str] = set()

    for raw_source_id in selected_sources:
        source_id = str(raw_source_id)
        if source_id in seen_sources or source_id not in available:
            continue
        allowed = (
            capability_resolver(source_id, "search", None)
            if capability_resolver
            else True
        )
        if allowed is False:
            continue
        seen_sources.add(source_id)
        targets.append({"source_id": source_id})

    for channel in channels:
        if not channel.get("enabled", True):
            continue
        channel_id = str(channel.get("id") or "")
        if not channel_id or channel_id in seen_channels:
            continue
        if selected_channel_ids is not None and channel_id not in selected_channel_ids:
            continue
        source_id = str(channel.get("source_id") or "")
        if source_id not in available:
            continue
        default_allowed = channel.get("mode") not in {
            "embed_only", "manual", "setup_required",
        }
        allowed = (
            capability_resolver(source_id, "scan_channel", channel)
            if capability_resolver
            else default_allowed
        )
        if allowed is False:
            continue
        seen_channels.add(channel_id)
        targets.append(
            {
                "source_id": source_id,
                "channel_id": channel_id,
                "channel_url": channel.get("url"),
                "channel_label": channel.get("label") or channel.get("url"),
            }
        )

    return targets
