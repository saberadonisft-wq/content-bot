import importlib.util
from pathlib import Path
from types import ModuleType

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "mediacrawler_runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("content_bot_mediacrawler_runner", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_uses_visible_persistent_browser(tmp_path: Path) -> None:
    runner = load_runner()
    config = ModuleType("config")

    runner.configure_media_crawler(config, tmp_path / "profiles")

    assert config.ENABLE_CDP_MODE is False
    assert config.CDP_CONNECT_EXISTING is False
    assert config.HEADLESS is False
    assert config.CDP_HEADLESS is False
    assert config.SAVE_LOGIN_STATE is True
    assert config.USER_DATA_DIR == str((tmp_path / "profiles" / "%s").resolve())


def test_runner_detects_existing_login_without_returning_cookie_values() -> None:
    runner = load_runner()
    assert runner.has_login_cookie("bili", [{"name": "DedeUserID", "value": "123"}])
    assert runner.has_login_cookie("dy", [{"name": "LOGIN_STATUS", "value": "1"}])
    assert not runner.has_login_cookie("dy", [{"name": "LOGIN_STATUS", "value": "0"}])
