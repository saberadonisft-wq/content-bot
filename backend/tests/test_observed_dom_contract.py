from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.crawlers.adapters.browser_video_dom import BrowserVideoDomContract
from app.crawlers.adapters.weibo import WeiboDomContract
from app.crawlers.observed_dom_contract import (
    DOM_CONTRACT_SCHEMA,
    DomContractUnavailable,
    load_observed_dom_contract,
)

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


def payload(source_id: str = "xhs") -> dict:
    provider_id = f"cbce_{source_id}"
    if source_id == "weibo":
        mode = "weibo_post_v1"
        template = "https://s.weibo.com/weibo?q={query}&page={page}"
        selectors = {
            "root_selector": "main.results",
            "card_selector": "article.post",
            "link_selector": "a.permalink",
            "text_selector": "div.body",
            "author_selector": "a.author",
            "timestamp_selector": "time.published",
            "next_selector": "a.next",
            "metric_selectors": {"like_count": "span.likes"},
            "image_selector": "img.content",
        }
    else:
        mode = "browser_video_v1"
        template = "https://www.xiaohongshu.com/search_result?keyword={query}&page={page}"
        selectors = {
            "root_selector": "main.results",
            "card_selector": "article.note",
            "link_selector": "a.note",
            "title_selector": "h2.title",
            "next_selector": "button.next",
            "metric_selectors": {"like_count": "span.likes"},
        }
    return {
        "schema_version": DOM_CONTRACT_SCHEMA,
        "source_id": source_id,
        "provider_id": provider_id,
        "mode": mode,
        "observed_at": (NOW - timedelta(hours=1)).isoformat(),
        "evidence_digest": "a" * 64,
        "reviewed_by": "crawler-reviewer",
        "search_url_template": template,
        "login_selectors": [],
        "selectors": selectors,
    }


def write_contract(root: Path, value: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{value['source_id']}.search.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_loads_reviewed_video_contract_and_encodes_query(tmp_path: Path) -> None:
    write_contract(tmp_path, payload())

    contract = load_observed_dom_contract(tmp_path, "xhs", now=NOW)

    assert isinstance(contract.selectors, BrowserVideoDomContract)
    assert contract.provider_id == "cbce_xhs"
    assert contract.build_search_url("game dev", 2).endswith(
        "keyword=game+dev&page=2"
    )
    assert len(contract.artifact_digest) == 64


def test_loads_weibo_contract_with_separate_schema(tmp_path: Path) -> None:
    write_contract(tmp_path, payload("weibo"))

    contract = load_observed_dom_contract(tmp_path, "weibo", now=NOW)

    assert isinstance(contract.selectors, WeiboDomContract)
    assert contract.build_search_url("游戏", 1).startswith(
        "https://s.weibo.com/weibo?q="
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value.update({"cookie": "secret"}), "SCHEMA_INVALID"),
        (lambda value: value.update({"provider_id": "legacy_bridge"}), "IDENTITY_INVALID"),
        (lambda value: value.update({"reviewed_by": ""}), "PROVENANCE_INVALID"),
        (
            lambda value: value.update(
                {"observed_at": (NOW + timedelta(hours=1)).isoformat()}
            ),
            "PROVENANCE_INVALID",
        ),
        (
            lambda value: value.update(
                {"search_url_template": "https://evil.example/search?q={query}"}
            ),
            "SEARCH_URL_INVALID",
        ),
        (
            lambda value: value["selectors"]["metric_selectors"].update(
                {"password": "span.secret"}
            ),
            "SELECTOR_SCHEMA_INVALID",
        ),
    ],
)
def test_contract_fails_closed_on_unreviewed_or_unsafe_fields(
    tmp_path: Path, mutate, reason: str
) -> None:
    value = payload()
    mutate(value)
    write_contract(tmp_path, value)

    with pytest.raises(DomContractUnavailable) as captured:
        load_observed_dom_contract(tmp_path, "xhs", now=NOW)

    assert reason in captured.value.reason_code


def test_contract_rejects_symlink_escape_and_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(DomContractUnavailable) as captured:
        load_observed_dom_contract(missing, "xhs", now=NOW)
    assert captured.value.reason_code == "DOM_CONTRACT_MISSING"

    outside = write_contract(tmp_path / "outside", payload())
    root = tmp_path / "root"
    root.mkdir()
    link = root / "xhs.search.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is not available")
    with pytest.raises(DomContractUnavailable) as captured:
        load_observed_dom_contract(root, "xhs", now=NOW)
    assert captured.value.reason_code == "DOM_CONTRACT_PATH_INVALID"
