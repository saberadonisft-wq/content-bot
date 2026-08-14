from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.crawlers.adapters.tiktok import TikTokTokenBundle, TikTokTokenVault


def bundle(access="access-token-value", refresh="refresh-token-value"):
    return TikTokTokenBundle(
        open_id="authorized-open-id-1234",
        scopes=frozenset({"user.info.basic", "video.list"}),
        access_token=access,
        refresh_token=refresh,
        expires_in=3600,
        refresh_expires_in=86_400,
    )


def test_tiktok_vault_encrypts_tokens_and_round_trips(tmp_path) -> None:
    root = tmp_path / "crawler-secrets"
    vault = TikTokTokenVault(root)
    issued = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)

    stored = vault.save(bundle(), authorized_username="@Game.Dev", issued_at=issued)
    loaded = vault.load()

    assert loaded == stored
    assert loaded is not None
    assert loaded.authorized_username == "game.dev"
    assert loaded.access_expires_at == issued + timedelta(hours=1)
    assert loaded.refresh_expires_at == issued + timedelta(days=1)
    assert loaded.access_token == "access-token-value"
    assert loaded.refresh_token == "refresh-token-value"
    ciphertext = vault.token_path.read_bytes()
    assert b"access-token-value" not in ciphertext
    assert b"refresh-token-value" not in ciphertext
    assert b"authorized-open-id-1234" not in ciphertext
    assert vault.key_path.read_bytes() not in ciphertext
    assert "access-token-value" not in repr(loaded)
    assert "refresh-token-value" not in repr(loaded)


def test_tiktok_vault_rotates_tokens_atomically(tmp_path) -> None:
    vault = TikTokTokenVault(tmp_path / "crawler-secrets")
    key_before = None
    vault.save(bundle())
    key_before = vault.key_path.read_bytes()
    first_ciphertext = vault.token_path.read_bytes()

    vault.save(bundle("access-token-rotated", "refresh-token-rotated"))
    loaded = vault.load()

    assert loaded is not None
    assert loaded.access_token == "access-token-rotated"
    assert loaded.refresh_token == "refresh-token-rotated"
    assert vault.key_path.read_bytes() == key_before
    assert vault.token_path.read_bytes() != first_ciphertext
    assert not list(vault.root.glob(".tiktok-*-*"))


def test_tiktok_vault_detects_ciphertext_tampering_without_leaking(tmp_path) -> None:
    vault = TikTokTokenVault(tmp_path / "crawler-secrets")
    vault.save(bundle())
    raw = bytearray(vault.token_path.read_bytes())
    raw[-8] ^= 1
    vault.token_path.write_bytes(raw)

    with pytest.raises(ValueError, match="cannot be decrypted") as raised:
        vault.load()
    assert "access-token-value" not in str(raised.value)


def test_tiktok_vault_delete_removes_ciphertext_but_not_shared_key(tmp_path) -> None:
    vault = TikTokTokenVault(tmp_path / "crawler-secrets")
    assert vault.load() is None
    vault.save(bundle())

    assert vault.delete() is True
    assert vault.delete() is False
    assert vault.load() is None
    assert vault.key_path.is_file()


def test_tiktok_vault_rejects_broad_root_and_missing_key(tmp_path) -> None:
    with pytest.raises(ValueError, match="too broad"):
        TikTokTokenVault(Path.cwd())

    vault = TikTokTokenVault(tmp_path / "crawler-secrets")
    vault.save(bundle())
    vault.key_path.unlink()
    with pytest.raises(ValueError, match="key is missing"):
        vault.load()
