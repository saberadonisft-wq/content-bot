from concurrent.futures import ThreadPoolExecutor

import pytest

from app.crawlers.runtime import IdentityPseudonymizer, PseudonymKeyStore


def test_pseudonym_key_store_creates_once_and_is_concurrency_safe(tmp_path) -> None:
    store = PseudonymKeyStore(tmp_path / "cbce-secrets")
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys = list(pool.map(lambda _: store.load_or_create(), range(8)))
    assert len({key for key in keys}) == 1
    assert len(keys[0]) == 32
    assert store.path.read_bytes() == keys[0]
    pseudonymizer = IdentityPseudonymizer(keys[0])
    pseudonym = pseudonymizer.pseudonym("bilibili", "123")
    assert pseudonym != "123"
    assert pseudonym.startswith("bilibili_")
    assert len(pseudonym) == len("bilibili_") + 24
    assert pseudonymizer.pseudonym("bilibili", "123") == pseudonym
    assert pseudonymizer.pseudonym("bilibili", "124") != pseudonym


def test_pseudonym_key_store_rejects_invalid_reference_and_broad_root(tmp_path) -> None:
    invalid = tmp_path / "invalid.key"
    invalid.write_bytes(b"short")
    with pytest.raises(ValueError, match="invalid"):
        PseudonymKeyStore.load_reference(invalid)
    with pytest.raises(ValueError, match="broad"):
        PseudonymKeyStore(tmp_path.cwd())
