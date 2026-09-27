"""Opt-in metadata canary for a multi-creator acquisition run.

Uses temporary SQLite storage and no download submission. Public mode does not
use browser cookies; an explicit Bilibili ``--connection-id`` may be supplied
to exercise the already logged-in CBCE profile without printing the ref.
Run from backend with the project interpreter; targets must be supplied explicitly.
"""

import argparse
import json
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.services.acquisition import AcquisitionManager
from app.services.connectors import default_connectors
from app.sqlite_store import SQLiteStore


class MetadataOnlyDownloads:
    def list(self):
        return []


def _is_bilibili_target(value):
    try:
        host = (urlsplit(value).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host in {"bilibili.com", "bilibili.tv", "b23.tv"} or any(
        host.endswith(root) for root in (".bilibili.com", ".bilibili.tv")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--mode", choices=("creator", "playlist"), default="creator")
    parser.add_argument(
        "--connection-id",
        help="Explicit Bilibili connection ref for a logged-in CBCE profile; never printed",
    )
    parser.add_argument("--max-candidates", type=int, default=40, choices=range(2, 101))
    parser.add_argument("--deadline-seconds", type=int, default=120, choices=range(30, 601))
    parser.add_argument("--continue-once", action="store_true",
                        help="For one supported creator/playlist target, fetch one additional page")
    args = parser.parse_args()
    single_playlist = args.mode == "playlist" and len(args.target) == 1
    if not 2 <= len(args.target) <= 20 and not (
        args.continue_once and len(args.target) == 1
    ) and not single_playlist:
        parser.error(
            "Supply 2 to 20 public targets, one playlist target, or one target with --continue-once"
        )
    if args.continue_once and len(args.target) != 1:
        parser.error("--continue-once accepts exactly one supported creator/playlist target")
    connection_id = (args.connection_id or "").strip()
    if connection_id and any(not _is_bilibili_target(target) for target in args.target):
        parser.error("--connection-id chỉ được dùng với target Bilibili")
    with tempfile.TemporaryDirectory(prefix="acquisition-batch-canary-") as directory:
        store = SQLiteStore(Path(directory) / "metadata.db")
        store.initialize()
        manager = AcquisitionManager(
            store,
            MetadataOnlyDownloads(),
            max_workers=1,
            connectors=default_connectors() if connection_id else None,
        )
        run_id = None
        started = time.monotonic()
        try:
            run = manager.create_run({
                "mode": args.mode, "targets": args.target,
                "source_id": "bilibili" if connection_id else None,
                "connection_id": connection_id or None,
                "limits": {"max_candidates": args.max_candidates,
                           "max_pages": 20, "deadline_seconds": args.deadline_seconds},
            })
            run_id = run["id"]
            while run["state"] in {"queued", "running"}:
                if time.monotonic() - started > args.deadline_seconds + 20:
                    raise TimeoutError("Batch supervisor did not finish within its deadline")
                time.sleep(0.25)
                run = manager.get_run(run_id)
            initial_count = len(manager.list_candidates(run_id, limit=100)["items"])
            continuation_state = None
            if args.continue_once:
                current = manager.get_run(run_id)
                if not current.get("can_continue"):
                    raise RuntimeError("The target did not expose a continuation cursor")
                manager.continue_run(run_id, expected_generation=current["pagination"]["generation"])
                while manager.get_run(run_id)["state"] in {"queued", "running"}:
                    if time.monotonic() - started > args.deadline_seconds * 2 + 20:
                        raise TimeoutError("Continuation did not finish within its deadline")
                    time.sleep(0.25)
                continuation_state = manager.get_run(run_id)
            candidates = manager.list_candidates(run_id, limit=100)["items"]
            valid_metadata = all(item.get("title") and item.get("canonical_url") for item in candidates)
            print(json.dumps({
                "state": run["state"], "duration_seconds": round(time.monotonic() - started, 2),
                "candidate_count": len(candidates), "valid_metadata": bool(valid_metadata),
                "initial_candidate_count": initial_count,
                "continued_candidate_count": len(candidates) if args.continue_once else None,
                "continuation_stop_reason": (
                    (continuation_state or {}).get("pagination", {}).get("last_stop_reason")
                    if continuation_state else None
                ),
                "children": [{key: child.get(key) for key in (
                    "target", "state", "provider_id", "error_code", "counters", "stop_reason",
                )} for child in run.get("children", [])],
                "downloads_submitted": 0,
                "connection_profile_used": bool(connection_id),
            }, ensure_ascii=True, indent=2))
            return 0 if run["state"] == "completed" and candidates and valid_metadata else 1
        finally:
            if run_id is not None:
                manager.cancel(run_id)
            manager.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
