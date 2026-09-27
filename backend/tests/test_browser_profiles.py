import json
from pathlib import Path

import pytest

from app.crawlers import SOURCE_REGISTRY
from app.crawlers.runtime import ProfileInUse, ProfileLock, ProfileNamespace


def namespace(tmp_path: Path) -> ProfileNamespace:
    return ProfileNamespace(
        tmp_path / "browser-profiles-v2",
        SOURCE_REGISTRY,
        forbidden_roots=(tmp_path / "browser-profile",),
    )


def test_profile_namespace_uses_canonical_source_and_hashed_account(tmp_path: Path) -> None:
    profiles = namespace(tmp_path)
    profile = profiles.profile("bili", "Private User@example.com")

    assert profile.source_id == "bilibili"
    assert profile.path.parent.name == "bilibili"
    assert profile.path.name == profile.account_key
    assert len(profile.account_key) == 24
    assert "private" not in str(profile.path).casefold()
    assert profile.path.is_dir()


def test_profile_namespace_rejects_legacy_overlap_and_path_like_source(tmp_path: Path) -> None:
    legacy = tmp_path / "browser-profile"
    with pytest.raises(ValueError, match="overlaps"):
        ProfileNamespace(legacy / "v2", SOURCE_REGISTRY, forbidden_roots=(legacy,))
    profiles = namespace(tmp_path)
    with pytest.raises(ValueError, match="Unknown or unsafe"):
        profiles.profile("../../youtube")


def test_profile_namespace_hashes_connection_owned_account_refs(tmp_path: Path) -> None:
    profiles = namespace(tmp_path)

    first = profiles.profile("bilibili", " Account One ")
    same = profiles.profile("bilibili", "account   one")
    second = profiles.profile("bilibili", "Account Two")

    assert first.account_key == same.account_key
    assert first.path == same.path
    assert first.account_key != second.account_key
    assert "account one" not in str(first.path)


def test_profile_lock_allows_one_owner_and_releases_cleanly(tmp_path: Path) -> None:
    profile = namespace(tmp_path).profile("youtube")
    first = ProfileLock(profile, owner_id="run-one")
    second = ProfileLock(profile, owner_id="run-two")

    first.acquire()
    assert first.acquired is True
    with pytest.raises(ProfileInUse):
        second.acquire()
    first.release()
    metadata = json.loads((profile.path / ".cbce-profile.lock").read_bytes()[1:])
    assert metadata["owner_id"] == "run-one"
    assert metadata["source_id"] == "youtube"
    assert "cookie" not in metadata

    second.acquire()
    assert second.acquired is True
    second.release()
    assert second.acquired is False


def test_profile_lock_context_manager_releases_after_exception(tmp_path: Path) -> None:
    profile = namespace(tmp_path).profile("reddit", "account-a")
    lock = ProfileLock(profile, owner_id="failing-run")
    with pytest.raises(RuntimeError, match="synthetic"), lock:
        raise RuntimeError("synthetic")
    assert lock.acquired is False
    with ProfileLock(profile, owner_id="next-run") as next_lock:
        assert next_lock.acquired is True


def test_profile_deletion_requires_exact_confirmation_and_refuses_active_profile(
    tmp_path: Path,
) -> None:
    profiles = namespace(tmp_path)
    profile = profiles.profile("bili", "account-a")
    (profile.path / "owned-state.txt").write_text("state", encoding="utf-8")
    with pytest.raises(ValueError, match="DELETE bilibili PROFILES"):
        profiles.delete_source_profiles("bili", confirmation="yes")

    lock = ProfileLock(profile, owner_id="active-run")
    lock.acquire()
    try:
        with pytest.raises(ProfileInUse):
            profiles.delete_source_profiles(
                "bilibili", confirmation="DELETE bilibili PROFILES"
            )
    finally:
        lock.release()

    assert profiles.delete_source_profiles(
        "bili", confirmation="DELETE bilibili PROFILES"
    ) == 1
    assert not profile.path.parent.exists()


def test_profile_namespace_fails_closed_while_source_deletion_is_pending(
    tmp_path: Path,
) -> None:
    profiles = namespace(tmp_path)
    profile = profiles.profile("xhs")
    marker = profile.path.parent / ".cbce-delete-pending"
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(ProfileInUse, match="pending deletion"):
        profiles.profile("xhs", "another-account")
    with pytest.raises(ProfileInUse, match="pending deletion"):
        ProfileLock(profile, owner_id="late-run").acquire()
