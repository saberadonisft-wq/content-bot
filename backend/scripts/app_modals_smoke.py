"""Check on-demand modal loading in the real app using browser-local API fixtures."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument("--output", type=Path, default=Path("artifacts/refactor-modules/modal-browser.json"))
    args = parser.parse_args()
    user = {"id": "test", "email": "test@example.com", "display_name": "Test", "role": "admin", "status": "approved", "auth_provider": "email", "created_at": "2026-09-12T00:00:00Z"}
    errors, requests = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: (errors.append(str(error)), print(str(error), flush=True)))
        page.on("request", lambda request: requests.append(request.url))
        page.add_init_script("localStorage.setItem('content_bot_access_token','fixture'); localStorage.setItem('content_bot_user'," + json.dumps(json.dumps(user)) + ");")

        def intercept(route):
            request = route.request
            parsed = urlparse(request.url)
            if parsed.netloc == urlparse(args.url).netloc:
                route.continue_()
                return
            if parsed.path.endswith("/auth/me"):
                data = user
            elif parsed.path.endswith(("/sources", "/keywords", "/admin/users", "/videos")):
                data = []
            elif parsed.path.endswith("/update/check"):
                data = {"update_available": True, "current_version": "1.0", "latest_version": "1.1"}
            elif parsed.path.endswith("/gemini/models"):
                data = {"models": []}
            elif parsed.path.endswith("/voiceover/status"):
                data = {"ready": False, "devices": [], "presets": [], "engines": []}
            elif parsed.path.endswith("/credentials/status"):
                data = {"is_master_password_set": False, "is_unlocked": False, "configured_keys": {},
                        "vault_configured_keys": {}, "env_configured_keys": {}, "credential_sources": {},
                        "masked_keys": {}, "platforms": {key: False for key in ("youtube", "x_twitter", "reddit", "meta_instagram", "facebook_page", "tiktok", "gemini")}}
            else:
                data = {}
            route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}, content_type="application/json", body=json.dumps(data))

        page.route("**/*", intercept)
        page.goto(args.url)
        settings = page.get_by_role("button", name="Cài đặt API", exact=True)
        settings.wait_for()
        assert not any("SettingsModal" in url for url in requests)
        assert not any("AdminUsersModal" in url for url in requests)
        settings.click()
        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        assert dialog.evaluate("node => getComputedStyle(node.parentElement).position") == "fixed"
        assert any("SettingsModal" in url for url in requests)
        page.get_by_role("button", name="Đóng cài đặt", exact=True).click()
        page.get_by_role("button", name="Quản lý User", exact=True).click()
        page.locator(".admin-modal").wait_for()
        assert page.locator(".admin-modal-overlay").evaluate("node => getComputedStyle(node).position") == "fixed"
        page.locator(".admin-modal-header button").click()
        page.get_by_role("button", name="Xem chi tiết & Hướng dẫn", exact=True).click()
        page.get_by_role("button", name="Đóng", exact=True).click()
        assert not any("ContentLibrary" in url for url in requests)
        page.get_by_role("button", name="Nguồn & Video", exact=True).click()
        page.get_by_role("heading", name="Nguồn & Video", exact=True).wait_for()
        assert any("ContentLibrary" in url for url in requests)
        page.get_by_role("button", name="Video", exact=True).click()
        page.locator(".video-library-embedded-head").wait_for()
        assert any("canva-" in url or "/canva.css" in url for url in requests)
        page.get_by_role("button", name="Phụ đề Video", exact=True).click()
        page.locator(".subtitle-studio-header").wait_for()
        assert page.locator(".subtitle-studio-header").evaluate("node => getComputedStyle(node).display") == "flex"
        page.get_by_role("button", name="Chuyển sang giao diện tối (Dark Studio)", exact=True).click()
        page.get_by_role("button", name="Chuyển sang giao diện sáng", exact=True).wait_for()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(args.output.with_suffix(".png")))
        anonymous = browser.new_page()
        anonymous.on("pageerror", lambda error: errors.append(str(error)))
        anonymous.route("**/*", intercept)
        anonymous.goto(args.url)
        anonymous.locator(".auth-card").wait_for()
        assert anonymous.locator(".auth-container").evaluate("node => getComputedStyle(node).display") == "flex"
        anonymous.close()
        user["status"] = "pending"
        pending = browser.new_page()
        pending.on("pageerror", lambda error: errors.append(str(error)))
        pending.add_init_script("localStorage.setItem('content_bot_access_token','fixture'); localStorage.setItem('content_bot_user'," + json.dumps(json.dumps(user)) + ");")
        pending.route("**/*", intercept)
        pending.goto(args.url)
        pending.locator(".pending-card").wait_for()
        assert pending.locator(".auth-container").evaluate("node => getComputedStyle(node).display") == "flex"
        pending.close()
        assert errors == [], errors
        report = {"browser": browser.version, "errors": errors, "lazy_settings_loaded": True,
                  "verified_views": ["settings", "admin", "update", "library", "videos", "studio", "studio_dark_theme", "login", "pending_approval"],
                  "settings_position": "fixed", "requests": requests}
        browser.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "lazy_settings_loaded": True}))


if __name__ == "__main__":
    main()
