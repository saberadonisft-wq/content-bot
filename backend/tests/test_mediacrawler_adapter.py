import json
import subprocess
import sys
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1] / "scripts" / "mediacrawler_adapter.py"


def test_adapter_normalizes_xhs_jsonl(tmp_path: Path) -> None:
    source = tmp_path / "xhs.jsonl"
    source.write_text(
        json.dumps(
            {
                "note_id": "abc123",
                "title": "Black Myth Wukong build",
                "desc": "boss guide",
                "nickname": "player",
                "liked_count": "1.2w",
                "comment_count": "42",
                "tag_list": "黑神话,攻略",
                "time": 1_700_000_000_000,
                "note_url": "https://www.xiaohongshu.com/explore/abc123",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(ADAPTER), "--source", "xhs", "--keywords", "wukong", "--input-jsonl", str(source)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    item = json.loads(completed.stdout)
    assert item["external_id"] == "abc123"
    assert item["metrics"]["like_count"] == 12_000
    assert item["hashtags"] == ["黑神话", "攻略"]


def test_adapter_normalizes_bilibili_metrics(tmp_path: Path) -> None:
    source = tmp_path / "bili.jsonl"
    source.write_text(
        json.dumps({"video_id": "99", "title": "Game review", "video_play_count": "5000", "video_comment": "80"}) + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(ADAPTER), "--source", "bili", "--keywords", "game", "--input-jsonl", str(source)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    item = json.loads(completed.stdout)
    assert item["canonical_url"] == "https://www.bilibili.com/video/av99"
    assert item["metrics"]["view_count"] == 5_000
    assert item["metrics"]["comment_count"] == 80
