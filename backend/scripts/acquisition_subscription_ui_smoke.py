"""Offline browser smoke for channel status/history; requires a Vite dev server."""

import argparse
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

HTML = r"""<!doctype html><html lang="vi"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><div id="root"></div>
<script type="module">
import R from '/@react-refresh'; R.injectIntoGlobalHook(window);
window.$RefreshReg$=()=>{}; window.$RefreshSig$=()=>type=>type;
window.__vite_plugin_react_preamble_installed__=true;
const source = await (await fetch('/src/features/library/VideoAcquisition.tsx')).text();
const reactPath = source.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {VideoAcquisition} = await import('/src/features/library/VideoAcquisition.tsx');
await import('/src/tokens.css'); await import('/src/styles.css');
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(VideoAcquisition));
</script></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5187")
    args = parser.parse_args()
    artifacts = Path(__file__).resolve().parents[2] / "artifacts" / "acquisition-ui"
    artifacts.mkdir(parents=True, exist_ok=True)
    channels = [{
        "id": f"channel-{index}", "source_id": "youtube",
        "canonical_url": f"https://www.youtube.com/@fixture{index}",
        "label": f"Kênh kiểm thử {index}", "target_kind": "creator", "enabled": True,
        "subscription": {
            "enabled": True, "initial_policy": "baseline", "auto_download": False,
            "interval_minutes": 360, "quality": "1080", "max_items": 20,
            "last_status": "waiting_capability", "last_error": "Nguồn cần đăng nhập lại.",
            "last_run_id": "history-outside-recent-list", "active_run_id": None,
            "last_scanned_at": "2026-09-23T03:00:00Z", "next_run_at": "2026-09-23T09:00:00Z",
        },
        "created_at": "2026-09-23T03:00:00Z", "updated_at": "2026-09-23T03:00:00Z",
    } for index in range(7)]
    run = {
        "id": "history-outside-recent-list", "source_id": "youtube", "provider_id": "yt-dlp",
        "mode": "creator", "state": "completed", "phase": "completed", "error": None,
        "error_code": None, "stop_reason": None,
        "request": {"targets": [channels[0]["canonical_url"]], "limits": {"max_candidates": 20},
                    "filters": {"published_after": "2026-09-22T10:00:00Z"}},
        "counters": {"scanned": 0, "new": 0, "duplicate": 0, "filtered": 0, "unavailable": 0},
        "pagination": {"kind": "youtube_position_v1", "generation": 0, "next_offset": 20,
                        "exhausted": False, "last_stop_reason": "candidate_budget"},
        "can_continue": True,
        "created_at": "2026-09-23T03:00:00Z", "updated_at": "2026-09-23T03:00:00Z",
    }
    candidates = [
        {
            "id": f"candidate-{index}", "external_id": f"fixture-{index}",
            "title": f"Video {index}", "source_id": "youtube", "provider_id": "yt-dlp",
            "canonical_url": f"https://www.youtube.com/watch?v=fixture{index}",
            "media_type": "video", "availability": "metadata", "download_available": True,
            "duration_seconds": 30, "uploader": "Fixture creator",
        }
        for index in range(101)
    ]
    unavailable = False
    opened_runs = []
    continue_requests = []
    unexpected = []
    batch_requests, batch_retries = [], []
    batch = {
        **run, "id": "batch-fixture", "provider_id": "batch", "state": "partial",
        "error": "1/3 kênh hoàn tất.", "error_code": "TARGETS_INCOMPLETE",
        "children": [{
            "id": f"batch-fixture:{index}", "target": channels[index]["canonical_url"],
            "state": state, "provider_id": "yt-dlp", "counters": {"scanned": 1 if index == 0 else 0, "new": 1 if index == 0 else 0},
            "error": None if index == 0 else "Nguồn tạm thời không phản hồi." if index == 1 else "Hết ngân sách metadata.",
            "error_code": None if index == 0 else "SOURCE_UNAVAILABLE" if index == 1 else "BUDGET_EXCEEDED",
        } for index, state in enumerate(("completed", "failed", "skipped"))],
    }

    def respond(route):
        path = urlsplit(route.request.url).path
        if path.endswith("/channels"):
            route.fulfill(status=503 if unavailable else 200,
                          json={"detail": "offline"} if unavailable else {"items": channels, "limit": 100})
        elif path.endswith("/capabilities"):
            route.fulfill(json={"version": 1, "feature_enabled": True, "items": []})
        elif path.endswith("/runs") and route.request.method == "POST":
            batch_requests.append(route.request.post_data_json)
            batch["request"] = route.request.post_data_json
            route.fulfill(status=202, json=batch)
        elif path.endswith("/batch-fixture/resume"):
            batch_retries.append(path)
            batch["children"][1].update(state="completed", error=None, error_code=None)
            batch["error"] = "2/3 kênh hoàn tất."
            route.fulfill(status=202, json=batch)
        elif path.endswith("/" + run["id"] + "/continue") and route.request.method == "POST":
            continue_requests.append(route.request.post_data_json)
            run["pagination"].update(generation=1, next_offset=40, exhausted=True,
                                      last_stop_reason="exhausted")
            run["can_continue"] = False
            route.fulfill(status=202, json=run)
        elif path.endswith("/batch-fixture"):
            route.fulfill(json=batch)
        elif path.endswith(("/runs", "/download-selections")):
            route.fulfill(json={"items": [], "limit": 50})
        elif path.endswith("/candidates"):
            query = parse_qs(urlsplit(route.request.url).query)
            offset = int((query.get("offset") or ["0"])[0])
            route.fulfill(json={
                "run_id": run["id"], "items": candidates[offset:offset + 100],
                "total": len(candidates), "offset": offset, "limit": 100,
            })
        elif path.endswith("/" + run["id"]):
            opened_runs.append(run["id"])
            route.fulfill(json=run)
        else:
            unexpected.append(path)
            route.fulfill(status=404, json={"detail": "Unexpected fixture request"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, timezone_id="Asia/Ho_Chi_Minh")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/__acquisition_smoke", lambda route: route.fulfill(content_type="text/html", body=HTML))
            page.route("**/api/v1/**", respond)
            page.clock.install()
            page.goto(args.base_url + "/__acquisition_smoke")
            expect(page.locator(".acquisition-channel-wrap")).to_have_count(6)
            expect(page.get_by_text("Chờ nguồn sẵn sàng", exact=True)).to_have_count(6)
            page.get_by_role("button", name="Xem thêm kênh (1 còn lại)").click()
            expect(page.locator(".acquisition-channel-wrap")).to_have_count(7)
            page.get_by_role("button", name="Xem lượt quét gần nhất").first.click()
            expect(page.get_by_role("heading", name="Kết quả xem trước")).to_be_visible()
            expect(page.get_by_label("Đăng sau", exact=True)).to_have_value("2026-09-22T17:00")
            assert opened_runs
            page.get_by_role("checkbox", name="Chọn Video 0", exact=True).check()
            page.get_by_role("button", name="Trang sau", exact=True).click()
            expect(page.get_by_role("heading", name="Kết quả xem trước")).to_be_visible()
            expect(page.get_by_role("checkbox", name="Chọn Video 100", exact=True)).to_be_visible()
            page.get_by_role("checkbox", name="Chọn Video 100", exact=True).check()
            expect(page.get_by_text("2 đã chọn trên các trang · 101 kết quả", exact=True)).to_be_visible()
            page.get_by_role("button", name="Cào thêm video", exact=True).click()
            expect(page.get_by_text("Đã hết kết quả trong cửa sổ nguồn.", exact=False)).to_be_visible()
            expect(page.get_by_role("button", name="Cào thêm video", exact=True)).to_have_count(0)
            assert continue_requests == [{"expected_generation": 0}]
            channels[0]["subscription"].update(last_status="succeeded", last_error=None)
            page.clock.fast_forward(15_100)
            expect(page.get_by_text("Quét gần nhất hoàn tất", exact=True)).to_be_visible()
            unavailable = True
            page.clock.fast_forward(15_100)
            expect(page.get_by_role("status")).to_contain_text("thông tin hiển thị có thể đã cũ")
            unavailable = False
            page.clock.fast_forward(15_100)
            expect(page.get_by_text("Chưa cập nhật được trạng thái kênh.", exact=False)).to_have_count(0)
            batch_targets = [channel["canonical_url"] for channel in channels[:3]]
            page.get_by_label("Link kênh hoặc playlist (mỗi dòng một link)", exact=True).fill("\n".join(batch_targets))
            page.get_by_role("button", name="Cào và xem trước", exact=True).click()
            expect(page.get_by_role("heading", name="Kết quả một phần")).to_be_visible()
            expect(page.get_by_role("list", name="Trạng thái từng kênh").locator("li")).to_have_count(3)
            assert batch_requests[0]["targets"] == batch_targets
            page.get_by_role("button", name="Thử lại kênh chưa hoàn tất").click()
            expect(page.get_by_text("Nguồn tạm thời không phản hồi.", exact=True)).to_have_count(0)
            assert len(batch_retries) == 1
            page.get_by_role("button", name="Đưa kênh chưa quét vào lượt mới").click()
            expect(page.get_by_label("Link kênh hoặc playlist (mỗi dòng một link)", exact=True)).to_have_value(batch_targets[2])
            assert len(batch_requests) == 1, "Preparing a new group must not auto-crawl"
            page.screenshot(path=str(artifacts / "desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile horizontal overflow"
            page.screenshot(path=str(artifacts / "mobile.png"), full_page=True)
            assert not errors, errors
            assert not unexpected, unexpected
            print("PASS: channel status/history, timezone, polling recovery, cross-page selection, batch partial/retry/skipped, desktop/mobile")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
