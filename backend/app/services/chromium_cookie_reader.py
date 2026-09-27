"""Extract and decrypt Chromium cookies (Chrome, Edge, Cốc Cốc, Brave) on Windows without launching a browser."""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("content_bot.chromium_cookie_reader")
SUPPORTED_BROWSERS = frozenset({"chrome", "edge", "coc_coc", "brave"})
_HOST_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$", re.IGNORECASE)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_unprotect(encrypted_bytes: bytes) -> bytes:
    """Decrypt Windows DPAPI protected data using CryptUnprotectData."""
    if platform.system() != "Windows":
        raise RuntimeError("DPAPI is only supported on Windows")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = DATA_BLOB()
    in_blob.cbData = len(encrypted_bytes)
    in_blob.pbData = ctypes.cast(
        (ctypes.c_byte * len(encrypted_bytes))(*encrypted_bytes),
        ctypes.POINTER(ctypes.c_byte),
    )

    out_blob = DATA_BLOB()

    ret = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ret:
        raise RuntimeError("CryptUnprotectData failed")

    result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    kernel32.LocalFree(out_blob.pbData)
    return result


def get_browser_user_data_paths() -> dict[str, Path]:
    """Find installed Chromium browser user data directories on Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return {}

    candidates = {
        "chrome": Path(local_app_data) / "Google" / "Chrome" / "User Data",
        "edge": Path(local_app_data) / "Microsoft" / "Edge" / "User Data",
        "coc_coc": Path(local_app_data) / "CocCoc" / "Browser" / "User Data",
        "brave": Path(local_app_data) / "BraveSoftware" / "Brave-Browser" / "User Data",
    }
    return {k: p for k, p in candidates.items() if p.is_dir()}


def _normalize_host(host_pattern: str) -> str:
    host = host_pattern.strip().lower().strip("%")
    if not host or len(host) > 253 or not _HOST_PATTERN.fullmatch(host):
        raise ValueError("Host cookie không hợp lệ")
    return host


def _cookie_databases(user_data_dir: Path) -> Iterable[Path]:
    profiles = [user_data_dir / "Default"]
    profiles.extend(sorted(user_data_dir.glob("Profile [0-9]*"), key=lambda path: path.name))
    for profile in profiles[:17]:
        for relative in (Path("Network") / "Cookies", Path("Cookies")):
            candidate = profile / relative
            if candidate.is_file():
                yield candidate


def get_chromium_master_key(user_data_dir: Path) -> bytes | None:
    """Read and decrypt the AES-256 master key from Local State."""
    local_state_path = user_data_dir / "Local State"
    if not local_state_path.is_file():
        return None

    try:
        data = json.loads(local_state_path.read_text(encoding="utf-8"))
        encrypted_key_b64 = data.get("os_crypt", {}).get("encrypted_key")
        if not encrypted_key_b64:
            return None

        encrypted_key = base64.b64decode(encrypted_key_b64)
        # Strip DPAPI prefix (5 bytes: 'DPAPI')
        if encrypted_key.startswith(b"DPAPI"):
            encrypted_key = encrypted_key[5:]

        return _dpapi_unprotect(encrypted_key)
    except Exception as exc:
        logger.debug("Failed to read Chromium master key from %s: %s", local_state_path, exc)
        return None


def decrypt_cookie_value(encrypted_val: bytes, master_key: bytes) -> str:
    """Decrypt a single encrypted cookie value (AES-256-GCM v10/v11)."""
    if not encrypted_val:
        return ""

    if encrypted_val.startswith((b"v10", b"v11")):
        # Format: 3 bytes prefix + 12 bytes nonce + ciphertext + 16 bytes tag
        nonce = encrypted_val[3:15]
        ciphertext_and_tag = encrypted_val[15:]
        try:
            aesgcm = AESGCM(master_key)
            decrypted = aesgcm.decrypt(nonce, ciphertext_and_tag, None)
            # Modern Chrome may prepend 32 bytes SHA-256 domain hash
            if len(decrypted) > 32 and not (32 <= decrypted[0] <= 126):
                decrypted = decrypted[32:]
            return decrypted.decode("utf-8", errors="replace")
        except Exception:
            return ""

    # Legacy DPAPI directly
    try:
        return _dpapi_unprotect(encrypted_val).decode("utf-8", errors="replace")
    except Exception:
        return ""


def read_cookies_for_host(
    host_pattern: str,
    *,
    browser_name: str | None = None,
    custom_profile_dir: Path | None = None,
) -> dict[str, str]:
    """Read and decrypt cookies matching a host pattern (e.g. '%douyin.com%', '%bilibili.com%').

    Copies SQLite database to temporary files first to avoid locking while the browser is running.
    """
    if platform.system() != "Windows":
        return {}

    host = _normalize_host(host_pattern)
    if browser_name is not None and browser_name not in SUPPORTED_BROWSERS:
        raise ValueError("Trình duyệt cookie không được hỗ trợ")

    user_data_dirs = get_browser_user_data_paths()
    if browser_name:
        if browser_name not in user_data_dirs:
            return {}
        targets = [user_data_dirs[browser_name]]
    elif custom_profile_dir:
        targets = [custom_profile_dir.expanduser().resolve()]
    else:
        # Check Cốc Cốc, Chrome, Edge in order
        order = ["coc_coc", "chrome", "edge", "brave"]
        targets = [user_data_dirs[b] for b in order if b in user_data_dirs]

    cookies: dict[str, str] = {}

    for u_dir in targets:
        master_key = get_chromium_master_key(u_dir)
        if not master_key:
            continue

        for cookie_file in _cookie_databases(u_dir):
            # Copy to temp dir to avoid database locked errors and never mutate a profile DB.
            with tempfile.TemporaryDirectory() as tmp:
                tmp_db = Path(tmp) / "Cookies"
                try:
                    shutil.copy2(cookie_file, tmp_db)
                    for ext in ("-wal", "-shm"):
                        wal_f = cookie_file.parent / (cookie_file.name + ext)
                        if wal_f.is_file():
                            shutil.copy2(wal_f, Path(tmp) / (tmp_db.name + ext))
                    conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
                    try:
                        integrity = conn.execute("PRAGMA integrity_check").fetchone()
                        if not integrity or integrity[0] != "ok":
                            logger.warning("Ignoring corrupt Chromium cookie DB %s", cookie_file)
                            continue
                        rows = conn.execute(
                            "SELECT name, CAST(encrypted_value AS BLOB) FROM cookies "
                            "WHERE host_key IN (?, ?) OR host_key LIKE ?",
                            (host, f".{host}", f"%.{host}"),
                        ).fetchall()
                    finally:
                        conn.close()
                    for name, enc_val in rows:
                        if enc_val:
                            val = decrypt_cookie_value(enc_val, master_key)
                            if val:
                                cookies[str(name)] = val
                except (OSError, sqlite3.DatabaseError) as exc:
                    logger.debug("Error reading SQLite cookies: %s", exc)

        if cookies:
            break

    return cookies


def get_cookie_header_string(host_pattern: str, browser_name: str | None = None) -> str:
    """Returns formatted 'name=value; name2=value2' Cookie header string for HTTP requests."""
    cookies = read_cookies_for_host(host_pattern, browser_name=browser_name)
    return "; ".join(f"{k}={v}" for k, v in cookies.items())
