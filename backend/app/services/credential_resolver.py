"""Resolve runtime credentials without mutating the application settings object."""

from __future__ import annotations

from typing import Any

from ..config import settings
from .credential_vault import get_vault


def credential(name: str, default: Any = None) -> Any:
    """Return a Vault value when unlocked, otherwise the value from ``.env``."""

    vault_value = get_vault().get_credential(name)
    if vault_value is not None:
        return vault_value
    value = getattr(settings, name, default)
    if isinstance(value, str):
        value = value.strip()
        return value or default
    return value


def credentials(*names: str) -> dict[str, Any]:
    """Resolve several credentials at one consistent point in time."""

    return {name: credential(name) for name in names}
