from __future__ import annotations

import os

from app.services.process_metrics import nvidia_memory_snapshot, process_rss_bytes


def test_current_process_rss_is_optional_but_safe():
    value = process_rss_bytes(os.getpid())
    assert value is None or value > 0


def test_nvidia_snapshot_is_optional_and_well_shaped():
    snapshot = nvidia_memory_snapshot()
    assert isinstance(snapshot, list)
    assert all(
        set(item) == {"index", "used_bytes", "total_bytes"}
        and item["used_bytes"] >= 0
        and item["total_bytes"] >= item["used_bytes"]
        for item in snapshot
    )
