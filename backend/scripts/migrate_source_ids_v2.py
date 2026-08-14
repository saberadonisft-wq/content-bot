"""Dry-run-first migration from legacy transport aliases to canonical source IDs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawlers import SOURCE_REGISTRY
from app.mongo import store

ALIASES = {"dy": "douyin", "ks": "kuaishou", "bili": "bilibili", "wb": "weibo"}


def collision_report() -> list[dict[str, str]]:
    collisions: list[dict[str, str]] = []
    for alias, canonical in ALIASES.items():
        for row in store.db.content_items.find(
            {"source_id": alias}, {"external_id": 1}
        ):
            external_id = str(row.get("external_id") or "")
            if external_id and store.db.content_items.find_one(
                {"source_id": canonical, "external_id": external_id}, {"_id": 1}
            ):
                collisions.append(
                    {"alias": alias, "canonical": canonical, "external_id": external_id}
                )
    return collisions


def plan() -> dict:
    return {
        "aliases": ALIASES,
        "content_items": {
            alias: store.db.content_items.count_documents({"source_id": alias})
            for alias in ALIASES
        },
        "source_runs": {
            alias: store.db.source_runs.count_documents({"source_id": alias})
            for alias in ALIASES
        },
        "keywords_with_legacy_ids": store.db.keywords.count_documents(
            {
                "$or": [
                    {"source_ids": {"$in": list(ALIASES)}},
                    {"channels.source_id": {"$in": list(ALIASES)}},
                ]
            }
        ),
        "collisions": collision_report(),
    }


def migrate_keywords() -> int:
    changed = 0
    for keyword in store.db.keywords.find({}):
        source_ids = [
            SOURCE_REGISTRY.resolve_id(source_id)
            for source_id in keyword.get("source_ids", [])
        ]
        source_ids = list(dict.fromkeys(source_ids))
        channels = [
            {
                **channel,
                "source_id": SOURCE_REGISTRY.resolve_id(
                    channel.get("source_id", "web")
                ),
            }
            for channel in keyword.get("channels", [])
        ]
        checkpoints = dict(keyword.get("source_checkpoints", {}))
        for alias, canonical in ALIASES.items():
            if alias in checkpoints and canonical not in checkpoints:
                checkpoints[canonical] = checkpoints[alias]
            checkpoints.pop(alias, None)
        if (
            source_ids != keyword.get("source_ids", [])
            or channels != keyword.get("channels", [])
            or checkpoints != keyword.get("source_checkpoints", {})
        ):
            store.db.keywords.update_one(
                {"_id": keyword["_id"]},
                {
                    "$set": {
                        "source_ids": source_ids,
                        "channels": channels,
                        "source_checkpoints": checkpoints,
                        "source_selection_version": 2,
                    }
                },
            )
            changed += 1
    return changed


def apply_migration(summary: dict) -> dict:
    if summary["collisions"]:
        raise RuntimeError(
            "Canonical content collisions exist; resolve them before applying. "
            "No documents were changed."
        )
    results = {"keywords": migrate_keywords()}
    for collection in ("content_items", "source_runs"):
        results[collection] = {}
        for alias, canonical in ALIASES.items():
            update = store.db[collection].update_many(
                {"source_id": alias}, {"$set": {"source_id": canonical}}
            )
            results[collection][alias] = update.modified_count
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate dy/ks/bili/wb to canonical Content Bot source IDs"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration. The default is a read-only dry run.",
    )
    args = parser.parse_args()
    store.initialize()
    summary = plan()
    output = {"mode": "apply" if args.apply else "dry_run", "plan": summary}
    if args.apply:
        output["changed"] = apply_migration(summary)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
