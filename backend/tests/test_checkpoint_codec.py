from datetime import UTC, datetime

import pytest

from app.services.checkpoints import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointTracker,
    cursor_scope,
    cursor_scope_key,
    query_fingerprint,
)

NOW = datetime(2026, 8, 12, 14, 30, tzinfo=UTC)


def fingerprint(*, terms=("Hades II", "Hades 2"), filters=None) -> str:
    return query_fingerprint(
        source_id="bluesky",
        provider="public-api",
        operation="search",
        terms=terms,
        target={"kind": "keyword"},
        filters=filters or {"sort": "latest", "language": "vi"},
        provider_version="2026-08",
    )


def tracker(checkpoint=None, *, recent_id_limit=3) -> CheckpointTracker:
    return CheckpointTracker(
        checkpoint,
        source_id="bluesky",
        provider="public-api",
        operation="search",
        query_fingerprint=fingerprint(),
        recent_id_limit=recent_id_limit,
        clock=lambda: NOW,
    )


def test_query_fingerprint_is_deterministic_and_normalizes_terms() -> None:
    first = fingerprint(
        terms=("  Hades   II ", "HADES II", "Hades 2"),
        filters={"language": "vi", "sort": "latest"},
    )
    second = fingerprint(
        terms=("hades ii", "hades 2"),
        filters={"sort": "latest", "language": "vi"},
    )

    assert first == second
    assert len(first) == 64
    assert first != fingerprint(filters={"sort": "top", "language": "vi"})
    assert first != query_fingerprint(
        source_id="reddit",
        provider="public-api",
        operation="search",
        terms=("hades ii", "hades 2"),
        target={"kind": "keyword"},
        filters={"sort": "latest", "language": "vi"},
        provider_version="2026-08",
    )


def test_cursor_scope_keys_are_hashed_stable_and_term_specific() -> None:
    first = cursor_scope("term", term="  Hades   II ", instance="api.bsky.app")
    equivalent = cursor_scope("TERM", instance="api.bsky.app", term="hades ii")
    alias = cursor_scope("term", term="Hades 2", instance="api.bsky.app")

    first_key = cursor_scope_key(first)
    assert first_key == cursor_scope_key(equivalent)
    assert first_key != cursor_scope_key(alias)
    assert first_key.startswith("scope_")
    assert len(first_key) == len("scope_") + 64
    assert "." not in first_key
    assert "$" not in first_key
    assert "hades" not in first_key


def test_per_scope_cursors_round_trip_without_colliding() -> None:
    current = tracker()
    primary = cursor_scope("term", term="Hades II")
    alias = cursor_scope("term", term="Hades 2")

    current.report_cursor(primary, {"cursor": "primary-page-2"})
    current.report_cursor(alias, {"cursor": "alias-page-4"})
    candidate = current.candidate()

    assert current.cursor(primary) == {"cursor": "primary-page-2"}
    assert current.cursor(alias) == {"cursor": "alias-page-4"}
    assert len(candidate["provider_cursors"]) == 2
    assert set(candidate["provider_cursors"]) == {
        cursor_scope_key(primary),
        cursor_scope_key(alias),
    }

    restored = tracker(candidate)
    assert restored.loaded_compatible is True
    assert restored.cursor(primary) == {"cursor": "primary-page-2"}
    assert restored.cursor(alias) == {"cursor": "alias-page-4"}


def test_report_none_removes_only_the_requested_cursor() -> None:
    current = tracker()
    primary = cursor_scope("term", term="Hades II")
    alias = cursor_scope("term", term="Hades 2")
    current.report_cursor(primary, "one")
    current.report_cursor(alias, "two")
    current.report_cursor(primary, None)

    assert current.cursor(primary) is None
    assert current.cursor(alias) == "two"


def test_observe_keeps_recent_ids_unique_newest_first_and_bounded() -> None:
    current = tracker(recent_id_limit=3)
    current.observe("post-1", "2026-08-10T01:00:00+07:00")
    current.observe("post-2", datetime(2026, 8, 10, 3, tzinfo=UTC))
    current.observe("post-3")
    current.observe("post-1", datetime(2026, 8, 9, tzinfo=UTC))
    current.observe("post-4", datetime(2026, 8, 11, tzinfo=UTC))

    candidate = current.candidate()
    assert candidate["recent_ids"] == ["post-4", "post-1", "post-3"]
    assert candidate["latest_published_at"] == "2026-08-11T00:00:00Z"
    assert current.latest_published_at == datetime(2026, 8, 11, tzinfo=UTC)


def test_candidate_changes_do_not_mutate_committed_state_until_commit() -> None:
    current = tracker()
    scope = cursor_scope("term", term="Hades II")
    original = current.committed()

    current.report_cursor(scope, "page-2")
    current.observe("post-1", NOW)
    candidate = current.candidate()

    assert current.dirty is True
    assert current.committed() == original
    assert current.committed()["provider_cursors"] == {}
    assert candidate["provider_cursors"]
    assert candidate["recent_ids"] == ["post-1"]
    assert candidate["observed_at"] == "2026-08-12T14:30:00Z"

    committed = current.commit_candidate(candidate)
    assert current.dirty is False
    assert current.committed() == committed
    assert current.cursor(scope) == "page-2"


def test_discard_candidate_restores_last_committed_checkpoint() -> None:
    current = tracker()
    scope = cursor_scope("term", term="Hades II")
    current.report_cursor(scope, "page-2")
    current.observe("post-1", NOW)
    current.commit_candidate()

    current.report_cursor(scope, "page-3")
    current.observe("post-2", datetime(2026, 8, 13, tzinfo=UTC))
    assert current.dirty is True

    current.discard_candidate()
    assert current.dirty is False
    assert current.cursor(scope) == "page-2"
    assert current.recent_ids == ("post-1",)
    assert current.latest_published_at == NOW


def test_incompatible_or_malformed_envelope_starts_clean() -> None:
    valid = tracker()
    valid.report_cursor(cursor_scope("term", term="Hades II"), "page-2")
    valid.observe("post-1", NOW)
    checkpoint = valid.candidate()
    checkpoint["query_fingerprint"] = "0" * 64

    reset = tracker(checkpoint)
    assert reset.loaded_compatible is False
    assert reset.recent_ids == ()
    assert reset.committed()["provider_cursors"] == {}
    assert reset.committed()["schema_version"] == CHECKPOINT_SCHEMA_VERSION


def test_loader_rejects_cursor_entry_whose_hash_does_not_match_scope() -> None:
    current = tracker()
    scope = cursor_scope("term", term="Hades II")
    current.report_cursor(scope, "page-2")
    checkpoint = current.candidate()
    entry = checkpoint["provider_cursors"].pop(cursor_scope_key(scope))
    checkpoint["provider_cursors"]["scope_" + "0" * 64] = entry

    restored = tracker(checkpoint)
    assert restored.loaded_compatible is True
    assert restored.cursor(scope) is None


def test_cursor_size_and_published_datetime_are_validated() -> None:
    current = tracker()
    scope = cursor_scope("term", term="Hades II")

    with pytest.raises(ValueError, match="exceeds"):
        current.report_cursor(scope, "x" * 20_000)
    with pytest.raises(ValueError, match="ISO-8601"):
        current.observe("post-1", "not-a-date")


def test_candidate_is_a_defensive_copy() -> None:
    current = tracker()
    scope = cursor_scope("term", term="Hades II")
    current.report_cursor(scope, {"cursor": "page-2"})

    candidate = current.candidate()
    candidate["recent_ids"].append("external-mutation")
    candidate["provider_cursors"][cursor_scope_key(scope)]["value"]["cursor"] = "changed"

    assert current.recent_ids == ()
    assert current.cursor(scope) == {"cursor": "page-2"}
