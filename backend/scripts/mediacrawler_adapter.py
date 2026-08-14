from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
from app.config import settings

MEDIACRAWLER_ROOT = PROJECT_ROOT / "vendor" / "mediacrawler"
DEFAULT_RUNTIME = MEDIACRAWLER_ROOT / ".venv" / "Scripts" / "python.exe"
RUNNER = PROJECT_ROOT / "backend" / "scripts" / "mediacrawler_runner.py"
PROFILE_ENV = "CONTENT_BOT_MEDIACRAWLER_PROFILE_DIR"
COCCOC_ENV = "CONTENT_BOT_COCCOC_EXECUTABLE_PATH"
RUN_ROOT_ENV = "CONTENT_BOT_MEDIACRAWLER_RUN_ROOT"
PLATFORM_URLS = {
    "xhs": "https://www.xiaohongshu.com/explore/{id}",
    "dy": "https://www.douyin.com/video/{id}",
    "ks": "https://www.kuaishou.com/short-video/{id}",
    "bili": "https://www.bilibili.com/video/av{id}",
    "wb": "https://m.weibo.cn/detail/{id}",
    "tieba": "https://tieba.baidu.com/p/{id}",
    "zhihu": "https://www.zhihu.com/question/{id}",
}


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace(",", "")
    multipliers = {
        "k": 1_000,
        "w": 10_000,
        "m": 1_000_000,
        "万": 10_000,
        "亿": 100_000_000,
    }
    for suffix, multiplier in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def first(payload: dict[str, Any], *keys: str) -> Any:
    return next(
        (payload[key] for key in keys if payload.get(key) not in (None, "")), None
    )


def published_at(payload: dict[str, Any]) -> str | None:
    value = first(
        payload, "time", "create_time", "publish_time", "created_at", "last_update_time"
    )
    if value is None:
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def tags(payload: dict[str, Any]) -> list[str]:
    raw = first(payload, "tag_list", "tags", "topics") or []
    if isinstance(raw, str):
        return [part.strip().lstrip("#") for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [
            str(item.get("name") if isinstance(item, dict) else item)
            .strip()
            .lstrip("#")
            for item in raw
            if item
        ]
    return []


def normalize(platform: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    external_id = first(payload, "note_id", "aweme_id", "video_id", "content_id", "id")
    if external_id is None:
        return None
    external_id = str(external_id)
    url = first(
        payload, "note_url", "video_url", "content_url", "url"
    ) or PLATFORM_URLS[platform].format(id=external_id)
    title = str(first(payload, "title", "question", "content", "desc") or "").strip()
    body = str(
        first(payload, "desc", "content", "description", "content_text") or ""
    ).strip()
    if not title:
        title = body[:180] or f"{platform}:{external_id}"
    return {
        "external_id": external_id,
        "canonical_url": str(url),
        "title": title[:500],
        "body_snippet": body[:4000],
        "author": str(
            first(payload, "nickname", "author", "user_name", "creator_name") or ""
        ),
        "hashtags": tags(payload),
        "locale": "zh-CN",
        "published_at": published_at(payload),
        "metrics": {
            "view_count": as_int(
                first(payload, "view_count", "video_play_count", "play_count")
            ),
            "like_count": as_int(
                first(payload, "liked_count", "like_count", "voteup_count")
            ),
            "comment_count": as_int(
                first(
                    payload,
                    "comment_count",
                    "comments_count",
                    "video_comment",
                    "answer_count",
                )
            ),
            "share_count": as_int(
                first(payload, "share_count", "shared_count", "video_share_count")
            ),
            "favorite_count": as_int(
                first(
                    payload, "collected_count", "favorite_count", "video_favorite_count"
                )
            ),
        },
        "raw_payload": payload,
    }


def emit_file(platform: str, path: Path) -> int:
    emitted = 0
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            try:
                normalized = normalize(platform, json.loads(line))
            except json.JSONDecodeError:
                continue
            if normalized:
                print(json.dumps(normalized, ensure_ascii=False), flush=True)
                emitted += 1
    return emitted


def run_crawler(args: argparse.Namespace) -> int:
    if not MEDIACRAWLER_ROOT.joinpath("main.py").exists():
        print(
            "MediaCrawler submodule is missing; run git submodule update --init --recursive",
            file=sys.stderr,
        )
        return 2
    runtime = Path(args.runtime) if args.runtime else DEFAULT_RUNTIME
    if (
        not runtime.exists()
        or not PROJECT_ROOT.joinpath("data", "mediacrawler-ready").exists()
    ):
        print(
            "MediaCrawler runtime is missing; run .\\scripts\\launcher.ps1 -Action setup-mediacrawler",
            file=sys.stderr,
        )
        return 3
    run_root = (
        Path(
            os.environ.get(RUN_ROOT_ENV) or PROJECT_ROOT / "data" / "mediacrawler-runs"
        )
        .expanduser()
        .resolve()
    )
    run_dir = run_root / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(
        args.profile_dir or PROJECT_ROOT / "data" / "browser-profile"
    ).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    coccoc_path = (
        Path(
            os.environ.get(COCCOC_ENV)
            or str(settings.content_bot_coccoc_executable_path)
        )
        .expanduser()
        .resolve()
    )
    if not coccoc_path.is_file():
        print(
            f"Coc Coc browser executable was not found: {coccoc_path}", file=sys.stderr
        )
        return 5
    command = [
        str(runtime),
        str(RUNNER),
        "--platform",
        args.source,
        "--lt",
        "qrcode",
        "--type",
        "search",
        "--keywords",
        args.keywords,
        "--get_comment",
        "no",
        "--get_sub_comment",
        "no",
        "--headless",
        "no",
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(run_dir),
        "--crawler_max_notes_count",
        str(min(args.max_items, 500)),
        "--max_concurrency_num",
        "1",
    ]
    environment = os.environ.copy()
    environment[PROFILE_ENV] = str(profile_dir)
    environment[COCCOC_ENV] = str(coccoc_path)
    print(
        f"Content Bot is opening Cốc Cốc for {args.source}; login state: {profile_dir}",
        file=sys.stderr,
    )
    completed = subprocess.run(
        command,
        cwd=MEDIACRAWLER_ROOT,
        env=environment,
        check=False,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    if completed.returncode:
        return completed.returncode
    files = sorted(run_dir.glob(f"{args.source}/jsonl/search_contents_*.jsonl"))
    if not files:
        print(
            f"MediaCrawler completed but produced no content JSONL under {run_dir}",
            file=sys.stderr,
        )
        return 4
    for file in files:
        emit_file(args.source, file)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Run MediaCrawler and emit Content Bot JSONL"
    )
    parser.add_argument("--source", required=True, choices=sorted(PLATFORM_URLS))
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--max-items", type=int, default=100)
    parser.add_argument("--profile-dir")
    parser.add_argument("--runtime")
    parser.add_argument(
        "--input-jsonl",
        type=Path,
        help="Normalize an existing MediaCrawler file without crawling",
    )
    args = parser.parse_args()
    if args.input_jsonl:
        emit_file(args.source, args.input_jsonl)
        return 0
    return run_crawler(args)


if __name__ == "__main__":
    raise SystemExit(main())
