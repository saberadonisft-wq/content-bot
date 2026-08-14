from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.crawlers.dom_contract_review import publish_reviewed_dom_contract
from app.crawlers.observed_dom_contract import load_observed_dom_contract


def _observation(**changes) -> dict:
    value = {
        "schema_version": "cbce.dom-structure.v1",
        "source_id": "xhs",
        "provider_id": "cbce_xhs",
        "operation": "search",
        "final_host": "www.xiaohongshu.com",
        "element_count": 100,
        "open_shadow_root_count": 0,
        "landmarks": [
            {"signature": "main.results", "count": 1},
            {"signature": "article.note", "count": 20},
        ],
        "attribute_names": [{"signature": "class", "count": 30}],
        "link_path_counts": [{"signature": "content", "count": 20}],
        "sentinel_counts": [{"signature": "challenge_1", "count": 0}],
        "observed_at": datetime(2026, 8, 13, 8, tzinfo=UTC).isoformat(),
    }
    value.update(changes)
    return value


def _draft(**changes) -> dict:
    value = {
        "search_url_template": (
            "https://www.xiaohongshu.com/search_result?keyword={query}&page={page}"
        ),
        "login_selectors": ["button.login-btn"],
        "selectors": {
            "root_selector": "main.results",
            "card_selector": "article.note",
            "link_selector": "a.note",
            "title_selector": "h2.title",
            "next_selector": "button.next",
            "metric_selectors": {"like_count": "span.likes"},
        },
    }
    value.update(changes)
    return value


def _write(path: Path, value: dict) -> bytes:
    raw = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(raw)
    return raw


def test_review_publishes_runtime_valid_contract_with_evidence_digest(
    tmp_path: Path,
) -> None:
    observation_path = tmp_path / "observation.json"
    draft_path = tmp_path / "draft.json"
    observation_raw = _write(observation_path, _observation())
    _write(draft_path, _draft())
    root = tmp_path / "contracts"

    destination = publish_reviewed_dom_contract(
        observation_path,
        draft_path,
        contract_root=root,
        reviewed_by="reviewer-2",
    )
    contract = load_observed_dom_contract(root, "xhs")

    assert destination == root / "xhs.search.json"
    assert contract.reviewed_by == "reviewer-2"
    assert contract.evidence_digest == hashlib.sha256(observation_raw).hexdigest()
    assert not list(root.glob(".*.tmp"))


@pytest.mark.parametrize(
    "changes",
    [
        {"cookie": "secret"},
        {"final_host": "evilxiaohongshu.com"},
        {"provider_id": "legacy_bridge"},
        {
            "landmarks": [
                {
                    "signature": "user_abcdefghijklmnopqrstuvwxyz0123456789",
                    "count": 1,
                }
            ]
        },
    ],
)
def test_review_rejects_non_value_free_or_wrong_identity_observation(
    tmp_path: Path, changes: dict
) -> None:
    observation_path = tmp_path / "observation.json"
    draft_path = tmp_path / "draft.json"
    _write(observation_path, _observation(**changes))
    _write(draft_path, _draft())

    with pytest.raises(ValueError):
        publish_reviewed_dom_contract(
            observation_path,
            draft_path,
            contract_root=tmp_path / "contracts",
            reviewed_by="reviewer-2",
        )


def test_invalid_draft_does_not_replace_existing_reviewed_contract(
    tmp_path: Path,
) -> None:
    observation_path = tmp_path / "observation.json"
    draft_path = tmp_path / "draft.json"
    _write(observation_path, _observation())
    _write(draft_path, _draft())
    root = tmp_path / "contracts"
    destination = publish_reviewed_dom_contract(
        observation_path,
        draft_path,
        contract_root=root,
        reviewed_by="reviewer-2",
    )
    original = destination.read_bytes()
    _write(
        draft_path,
        _draft(
            search_url_template="https://evil.example/?q={query}",
        ),
    )

    with pytest.raises(ValueError):
        publish_reviewed_dom_contract(
            observation_path,
            draft_path,
            contract_root=root,
            reviewed_by="reviewer-2",
        )

    assert destination.read_bytes() == original
