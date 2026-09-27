from __future__ import annotations

import sqlite3

import pytest

from app.services import chromium_cookie_reader as reader


def _cookie_db(path, rows):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    connection.executemany("INSERT INTO cookies VALUES (?, ?, ?)", rows)
    connection.commit()
    connection.close()


def test_cookie_reader_scopes_host_and_profiles_without_touching_source_db(tmp_path, monkeypatch):
    user_data = tmp_path / "Chrome" / "User Data"
    cookie_dir = user_data / "Default" / "Network"
    cookie_dir.mkdir(parents=True)
    source = cookie_dir / "Cookies"
    _cookie_db(source, [
        ("example.com", "session", b"one"),
        (".example.com", "subdomain", b"two"),
        ("child.example.com", "child", b"three"),
        ("notexample.com", "wrong", b"four"),
    ])
    monkeypatch.setattr(reader.platform, "system", lambda: "Windows")
    monkeypatch.setattr(reader, "get_browser_user_data_paths", lambda: {"chrome": user_data})
    monkeypatch.setattr(reader, "get_chromium_master_key", lambda _path: b"key")
    monkeypatch.setattr(reader, "decrypt_cookie_value", lambda value, _key: value.decode())

    result = reader.read_cookies_for_host("example.com", browser_name="chrome")
    assert result == {"session": "one", "subdomain": "two", "child": "three"}
    assert source.is_file()


def test_cookie_reader_rejects_unsupported_browser_and_host(monkeypatch):
    monkeypatch.setattr(reader.platform, "system", lambda: "Windows")
    with pytest.raises(ValueError, match="cookie"):
        reader.read_cookies_for_host("example.com", browser_name="opera")
    with pytest.raises(ValueError, match="Host"):
        reader.read_cookies_for_host("https://example.com")


def test_cookie_reader_ignores_corrupt_database(tmp_path, monkeypatch):
    user_data = tmp_path / "User Data"
    cookie_dir = user_data / "Default" / "Network"
    cookie_dir.mkdir(parents=True)
    (cookie_dir / "Cookies").write_bytes(b"not sqlite")
    monkeypatch.setattr(reader.platform, "system", lambda: "Windows")
    monkeypatch.setattr(reader, "get_browser_user_data_paths", lambda: {"chrome": user_data})
    monkeypatch.setattr(reader, "get_chromium_master_key", lambda _path: b"key")
    assert reader.read_cookies_for_host("example.com", browser_name="chrome") == {}
