from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.main import app
from app.routes import google

client = TestClient(app)


def _state_from_redirect(location: str) -> str:
    values = parse_qs(urlsplit(location).query).get("state")
    assert values
    return values[0]


def test_desktop_google_login_returns_once_to_exact_loopback(monkeypatch) -> None:
    monkeypatch.setattr(google.settings, "google_client_id", "test-client")
    callback = "http://127.0.0.1:49152/content-bot-google/" + ("a" * 43)

    start = client.get(
        "/auth/google",
        params={"desktop_callback": callback},
        follow_redirects=False,
    )

    assert start.status_code in {302, 307}
    state = _state_from_redirect(start.headers["location"])
    failed = client.get(
        "/auth/google/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert failed.status_code in {302, 307}
    target = urlsplit(failed.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == callback
    assert parse_qs(target.query) == {"auth_error": ["access_denied"]}

    replay = client.get(
        "/auth/google/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert replay.status_code in {302, 307}
    assert replay.headers["location"].startswith(google.settings.frontend_url)
    assert "kh%C3%B4ng+h%E1%BB%A3p+l%E1%BB%87" in replay.headers["location"]


def test_desktop_google_login_rejects_non_loopback_callbacks(monkeypatch) -> None:
    monkeypatch.setattr(google.settings, "google_client_id", "test-client")
    callbacks = [
        "https://attacker.example/content-bot-google/" + ("a" * 43),
        "http://localhost:49152/content-bot-google/" + ("a" * 43),
        "http://127.0.0.1:80/content-bot-google/" + ("a" * 43),
        "http://127.0.0.1:49152/other/" + ("a" * 43),
    ]

    for callback in callbacks:
        response = client.get("/auth/google", params={"desktop_callback": callback})
        assert response.status_code == 400
