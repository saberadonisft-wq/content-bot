from pathlib import Path

import pytest

from app.crawlers.worker import (
    BilibiliDetailWorkerRequest,
    BilibiliPagedTargetWorkerRequest,
    BilibiliWorkerRequest,
    TiebaTargetWorkerRequest,
    TiebaWorkerRequest,
    _request_from_payload,
)


def payload(tmp_path: Path):
    return {
        "action": "bilibili_search",
        "terms": ["game", "game", "indie"],
        "max_items": 10,
        "max_requests": 5,
        "deadline_seconds": 60,
        "initial_cursor": "v1:0:1:2",
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles-v2"),
        "pseudonym_key_ref": str(tmp_path / "key.ref"),
    }


def test_bilibili_worker_request_is_bounded_and_deduplicates_terms(tmp_path) -> None:
    request = BilibiliWorkerRequest.from_payload(payload(tmp_path))
    assert request.terms == ("game", "indie")
    assert request.max_items == 10
    assert request.initial_cursor == "v1:0:1:2"
    assert request.profile_root == (tmp_path / "profiles-v2").resolve()


def test_tieba_worker_request_uses_same_bounded_search_contract(tmp_path) -> None:
    values = payload(tmp_path)
    values["action"] = "tieba_search"
    request = _request_from_payload(values)

    assert isinstance(request, TiebaWorkerRequest)
    assert request.terms == ("game", "indie")
    assert request.max_items == 10


@pytest.mark.parametrize(
    ("action", "target", "operation"),
    [
        ("tieba_detail", "https://tieba.baidu.com/p/1234567890?pn=2", "fetch_detail"),
        ("tieba_forum", "https://tieba.baidu.com/f?kw=game", "scan_channel"),
        (
            "tieba_creator",
            "https://tieba.baidu.com/home/main?id=public-author",
            "list_creator",
        ),
        (
            "tieba_comments",
            "https://tieba.baidu.com/p/1234567890",
            "list_comments",
        ),
    ],
)
def test_tieba_target_worker_request_is_canonical_and_bounded(
    tmp_path, action, target, operation
) -> None:
    values = {
        "action": action,
        "target_url": target,
        "max_items": 4,
        "max_requests": 2,
        "deadline_seconds": 60,
        "initial_cursor": None,
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles-v2"),
        "pseudonym_key_ref": str(tmp_path / "key.ref"),
    }
    request = _request_from_payload(values)
    assert isinstance(request, TiebaTargetWorkerRequest)
    assert request.operation == operation
    assert request.target_url.startswith("https://tieba.baidu.com/")


def test_tieba_target_worker_rejects_cross_operation_target(tmp_path) -> None:
    values = {
        "action": "tieba_creator",
        "target_url": "https://tieba.baidu.com/p/1234567890",
        "max_items": 4,
        "max_requests": 2,
        "deadline_seconds": 60,
        "initial_cursor": None,
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles-v2"),
        "pseudonym_key_ref": str(tmp_path / "key.ref"),
    }
    with pytest.raises(ValueError, match="kind"):
        _request_from_payload(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "unknown"),
        ("terms", []),
        ("terms", ["x" * 201]),
        ("max_items", 0),
        ("max_items", 10_001),
        ("max_requests", 1_001),
        ("deadline_seconds", 7_201),
        ("initial_cursor", {"unsafe": True}),
        ("profile_root", ""),
    ],
)
def test_bilibili_worker_request_rejects_unbounded_or_invalid_input(
    tmp_path, field, value
) -> None:
    values = payload(tmp_path)
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        BilibiliWorkerRequest.from_payload(values)


def test_bilibili_detail_worker_request_canonicalizes_public_video(tmp_path) -> None:
    values = {
        "action": "bilibili_detail",
        "target_url": "https://m.bilibili.com/video/BV1ab411c7De?p=2",
        "deadline_seconds": 60,
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles-v2"),
        "pseudonym_key_ref": str(tmp_path / "key.ref"),
    }
    request = _request_from_payload(values)

    assert isinstance(request, BilibiliDetailWorkerRequest)
    assert request.target_url == "https://www.bilibili.com/video/BV1ab411c7De"


@pytest.mark.parametrize(
    "target",
    [
        "https://space.bilibili.com/123",
        "https://evilbilibili.com/video/BV1ab411c7De",
        "",
    ],
)
def test_bilibili_detail_worker_request_rejects_non_video_target(
    tmp_path, target
) -> None:
    values = {
        "action": "bilibili_detail",
        "target_url": target,
        "deadline_seconds": 60,
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles-v2"),
        "pseudonym_key_ref": str(tmp_path / "key.ref"),
    }
    with pytest.raises(ValueError):
        BilibiliDetailWorkerRequest.from_payload(values)


@pytest.mark.parametrize(
    ("action", "target", "operation", "root_id"),
    [
        (
            "bilibili_creator",
            "https://space.bilibili.com/123/upload/video",
            "list_creator",
            None,
        ),
        (
            "bilibili_comments",
            "https://www.bilibili.com/video/BV1ab411c7De?p=2",
            "list_comments",
            None,
        ),
        (
            "bilibili_child_comments",
            "https://www.bilibili.com/video/BV1ab411c7De",
            "list_child_comments",
            "8001",
        ),
    ],
)
def test_bilibili_paged_target_request_canonicalizes_and_selects_operation(
    tmp_path, action, target, operation, root_id
) -> None:
    values = {
        "action": action,
        "target_url": target,
        "max_items": 12,
        "max_requests": 3,
        "deadline_seconds": 60,
        "initial_cursor": None,
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles-v2"),
        "pseudonym_key_ref": str(tmp_path / "key.ref"),
    }
    if root_id is not None:
        values["root_comment_id"] = root_id

    request = _request_from_payload(values)

    assert isinstance(request, BilibiliPagedTargetWorkerRequest)
    assert request.operation == operation
    assert request.root_comment_id == root_id
    assert request.target_url in {
        "https://space.bilibili.com/123",
        "https://www.bilibili.com/video/BV1ab411c7De",
    }


@pytest.mark.parametrize(
    ("action", "target", "root_id"),
    [
        (
            "bilibili_creator",
            "https://www.bilibili.com/video/BV1ab411c7De",
            None,
        ),
        ("bilibili_comments", "https://space.bilibili.com/123", None),
        (
            "bilibili_child_comments",
            "https://www.bilibili.com/video/BV1ab411c7De",
            "invalid",
        ),
    ],
)
def test_bilibili_paged_target_request_rejects_wrong_target_or_root(
    tmp_path, action, target, root_id
) -> None:
    values = {
        "action": action,
        "target_url": target,
        "max_items": 12,
        "max_requests": 3,
        "deadline_seconds": 60,
        "initial_cursor": None,
        "browser_executable": str(tmp_path / "browser.exe"),
        "profile_root": str(tmp_path / "profiles-v2"),
        "pseudonym_key_ref": str(tmp_path / "key.ref"),
    }
    if root_id is not None:
        values["root_comment_id"] = root_id
    with pytest.raises(ValueError):
        BilibiliPagedTargetWorkerRequest.from_payload(values)
