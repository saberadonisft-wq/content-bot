from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def _item(store, *, source_id: str = "reddit", external_id: str = "abc123"):
    now = datetime.now(UTC)
    return store.save_item(
        {
            "source_id": source_id,
            "external_id": external_id,
            "canonical_url": f"https://www.reddit.com/comments/{external_id}",
            "title": "Thread",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )


def test_comment_upsert_is_idempotent_and_preserves_first_seen(mongo_store) -> None:
    _item(mongo_store)
    first_seen = datetime.now(UTC) - timedelta(hours=2)
    updated_at = datetime.now(UTC)
    first = mongo_store.save_comment(
        {
            "source_id": "reddit",
            "external_id": "root1",
            "content_external_id": "abc123",
            "body": "first body",
            "author_pseudonym": "reddit:anon",
            "published_at": first_seen,
            "like_count": 2,
            "child_count": 1,
            "parent_external_id": None,
            "root_external_id": "root1",
            "provenance": {"provider_id": "reddit_public"},
            "first_seen_at": first_seen,
            "last_seen_at": first_seen,
        }
    )
    second = mongo_store.save_comment(
        {
            "source_id": "reddit",
            "external_id": "root1",
            "content_external_id": "abc123",
            "body": "edited body",
            "author_pseudonym": "reddit:anon",
            "published_at": first_seen,
            "like_count": 5,
            "child_count": 1,
            "parent_external_id": None,
            "root_external_id": "root1",
            "provenance": {"provider_id": "reddit_public"},
            "last_seen_at": updated_at,
        }
    )

    assert second["id"] == first["id"]
    assert second["first_seen_at"] == first["first_seen_at"]
    assert abs((second["last_seen_at"] - updated_at).total_seconds()) < 0.001
    assert second["body"] == "edited body"
    assert second["like_count"] == 5
    assert mongo_store.db.comments.count_documents({}) == 1


def test_comment_hierarchy_query_and_content_delete_cascade(mongo_store) -> None:
    item = _item(mongo_store)
    now = datetime.now(UTC)
    for external_id, parent in (("root1", None), ("child1", "root1")):
        mongo_store.save_comment(
            {
                "source_id": "reddit",
                "external_id": external_id,
                "content_external_id": "abc123",
                "body": external_id,
                "published_at": now,
                "like_count": 0,
                "child_count": 0,
                "parent_external_id": parent,
                "root_external_id": "root1",
                "provenance": {},
            }
        )

    rows = mongo_store.comments_for_content("reddit", "abc123")
    assert {row["external_id"] for row in rows} == {"root1", "child1"}
    assert mongo_store.comments_for_content(
        "reddit", "abc123", root_external_id="root1"
    ) == rows

    mongo_store.delete_item(item["id"])
    assert mongo_store.comments_for_content("reddit", "abc123") == []


def test_comment_store_rejects_invalid_identity_and_self_parent(mongo_store) -> None:
    with pytest.raises(ValueError, match="identity"):
        mongo_store.save_comment(
            {
                "source_id": "reddit",
                "external_id": "",
                "content_external_id": "abc123",
            }
        )
    with pytest.raises(ValueError, match="own parent"):
        mongo_store.save_comment(
            {
                "source_id": "reddit",
                "external_id": "same",
                "content_external_id": "abc123",
                "parent_external_id": "same",
            }
        )
