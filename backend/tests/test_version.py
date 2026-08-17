from __future__ import annotations

from app.version import APP_VERSION


def test_app_version():
    assert isinstance(APP_VERSION, str)
    assert len(APP_VERSION.split(".")) >= 2
