"""Run a bounded, non-persisting browser-provider comparison."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings
from app.crawlers.runtime import PseudonymKeyStore, safe_diagnostic
from app.services.cbce_connectors import CbceBilibiliConnector, CbceTiebaConnector
from app.services.cbce_shadow import (
    ShadowProviderTimeout,
    compare_connectors_without_persistence,
)
from app.services.connectors import SearchQuery, default_connectors

RUN_ROOT_ENV = "CONTENT_BOT_MEDIACRAWLER_RUN_ROOT"
_CANDIDATES = {
    "bilibili": CbceBilibiliConnector,
    "tieba": CbceTiebaConnector,
}
_METRICS = {
    "bilibili": (
        "view_count",
        "like_count",
        "comment_count",
        "share_count",
        "favorite_count",
    ),
    "tieba": ("comment_count",),
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare legacy and CBCE browser search using aggregate-only output. "
            "No shadow item is written to Mongo or an export file."
        )
    )
    parser.add_argument("--source", choices=tuple(_CANDIDATES), default="bilibili")
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-items", type=int, default=20, choices=range(1, 51))
    parser.add_argument(
        "--provider-timeout-seconds",
        type=int,
        default=60,
        choices=range(15, 601),
    )
    parser.add_argument(
        "--acknowledge-visible-browsers",
        action="store_true",
        help="Required because the two providers may open visible Cốc Cốc windows.",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if not args.acknowledge_visible_browsers:
        raise ValueError("VISIBLE_BROWSER_ACK_REQUIRED")
    if not settings.content_bot_cbce_enabled:
        raise ValueError("CONTENT_BOT_CBCE_ENABLED_REQUIRED")
    source_id = str(getattr(args, "source", "bilibili"))
    legacy = default_connectors()[source_id]
    candidate = _CANDIDATES[source_id]()
    if isinstance(legacy, (CbceBilibiliConnector, CbceTiebaConnector)):
        raise TypeError("LEGACY_BASELINE_OVERRIDE_ACTIVE")
    key = PseudonymKeyStore(settings.data_dir / "cbce-secrets").load_or_create()
    scratch = _create_shadow_scratch()
    previous_run_root = os.environ.get(RUN_ROOT_ENV)
    os.environ[RUN_ROOT_ENV] = str(scratch)
    try:
        result = await compare_connectors_without_persistence(
            legacy,
            candidate,
            SearchQuery(
                keyword_id=0,
                name=args.query,
                include_terms=[args.query],
                max_items=args.max_items,
            ),
            secret_key=key,
            allowed_metric_keys=_METRICS[source_id],
            provider_timeout_seconds=args.provider_timeout_seconds,
        )
        return result.as_dict()
    finally:
        if previous_run_root is None:
            os.environ.pop(RUN_ROOT_ENV, None)
        else:
            os.environ[RUN_ROOT_ENV] = previous_run_root
        _remove_shadow_scratch(scratch)


def _create_shadow_scratch() -> Path:
    base = (settings.data_dir / "cbce-shadow-scratch").resolve()
    base.mkdir(parents=True, exist_ok=True)
    root = (base / uuid.uuid4().hex).resolve()
    if root.parent != base:
        raise ValueError("SHADOW_SCRATCH_PATH_INVALID")
    root.mkdir()
    return root


def _remove_shadow_scratch(root: Path) -> None:
    base = (settings.data_dir / "cbce-shadow-scratch").resolve()
    resolved = root.resolve()
    if resolved.parent != base or len(resolved.name) != 32:
        raise ValueError("SHADOW_SCRATCH_PATH_INVALID")
    shutil.rmtree(resolved, ignore_errors=True)


def main() -> int:
    args = _arguments()
    try:
        result = asyncio.run(_run(args))
    except ShadowProviderTimeout as exc:
        print(
            json.dumps(
                {
                    "error_code": exc.reason_code,
                    "error": "A shadow provider exceeded its bounded deadline.",
                    "persisted": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"error": safe_diagnostic(exc), "persisted": False},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
