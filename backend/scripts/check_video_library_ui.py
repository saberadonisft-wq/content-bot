"""Browser checks against fixture APIs; requires Vite at localhost:5173."""
from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[2]
    artifacts = root / "artifacts" / "video-library"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video-library-ui-", dir=root / "frontend") as harness, sync_playwright() as playwright:
        module = Path(harness) / "harness.tsx"
        module.write_text(
            "import React from 'react';import{createRoot}from'react-dom/client';"
            "import '/src/tokens.css';import '/src/styles.css';import{VideoLibrary}from'/src/VideoLibrary.tsx';"
            "createRoot(document.getElementById('root')).render(<main style={{maxWidth:1280,margin:'0 auto',padding:16}}><VideoLibrary/></main>);",
            encoding="utf-8",
        )
        html = '<html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><div id="root"></div><script type="module">import R from "/@react-refresh";R.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>(type)=>type;window.__vite_plugin_react_preamble_installed__=true;</script>' + f'<script type="module" src="/{Path(harness).name}/harness.tsx"></script></html>'
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/__video_library_harness", lambda route: route.fulfill(content_type="text/html", body=html))
        jobs = []
        requests = []
        deleted_requests = []
        failed_deletes = set()
        retries = []
        videos = [{
            "id": "a" * 32, "filename": "a" * 32 + ".mp4", "title": "Khám phá thế giới Neverness to Everness",
            "type": "original", "downloaded": True, "platform": "Bilibili", "duration": 83,
            "source_url": "https://www.bilibili.com/video/BVexample", "size_bytes": 1024 ** 2 * 18,
            "created_at": "2026-09-14T09:00:00Z", "thumbnail_url": "/api/v1/videos/thumbnail", "video_url": "/api/v1/subtitles/video/" + "a" * 32,
        }, {
            "id": "b" * 32, "filename": "video-tai-len.mp4", "type": "original", "downloaded": False,
            "size_bytes": 1024 ** 2 * 10, "created_at": "2026-09-13T09:00:00Z",
            "thumbnail_url": "/api/v1/videos/thumbnail", "video_url": "/api/v1/subtitles/video/" + "b" * 32,
        }]

        def respond(route):
            path = urlparse(route.request.url).path
            if path.endswith("/thumbnail"):
                route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#d7e4ee"/><path d="M0 300L200 140L350 260L500 100L640 250V360H0Z" fill="#9db5c9"/></svg>')
                return
            if path.endswith("/downloads") and route.request.method == "POST":
                payload = route.request.post_data_json
                requests.append(payload)
                result = []
                for index, url in enumerate(payload["urls"]):
                    job = {"id": str(len(jobs) + index).zfill(32), "url": url, "quality": payload["quality"],
                           "platform": "Bilibili", "state": "running", "phase": "downloading", "progress": 42,
                           "title": "Video đang tải", "filename": "", "downloaded_bytes": 1024 ** 2 * 12,
                           "total_bytes": 1024 ** 2 * 30, "speed": 1024 ** 2 * 2, "eta": 9, "duration": 83,
                           "error": None, "created_at": "2026-09-14T10:00:00Z", "updated_at": "2026-09-14T10:00:00Z"}
                    jobs.append(job)
                    result.append(job)
                route.fulfill(status=202, json=result)
                return
            if path.endswith("/cancel"):
                job_id = path.split("/")[-2]
                job = next(job for job in jobs if job["id"] == job_id)
                job.update(state="canceled", phase="canceled")
                route.fulfill(json=job)
                return
            if path.endswith("/retry"):
                job_id = path.split("/")[-2]
                retries.append(job_id)
                job = next(job for job in jobs if job["id"] == job_id)
                job.update(state="running", phase="resuming")
                route.fulfill(status=202, json=job)
                return
            if path.endswith("/downloads"):
                route.fulfill(json=jobs)
                return
            if path.endswith("/videos"):
                route.fulfill(json=videos)
                return
            if route.request.method == "DELETE":
                video_id = path.split("/")[-1]
                video_type = parse_qs(urlparse(route.request.url).query)["type"][0]
                deleted_requests.append((video_type, video_id))
                if (video_type, video_id) in failed_deletes:
                    route.fulfill(status=500, json={"detail": "File đang được sử dụng."})
                    return
                videos[:] = [video for video in videos if (video["type"], video["id"]) != (video_type, video_id)]
                route.fulfill(status=204)
                return
            route.fulfill(status=404, json={"detail": "Fixture route unavailable"})

        page.route("**/api/v1/**", respond)
        page.goto("http://127.0.0.1:5173/__video_library_harness")
        expect(page.locator(".vl-card")).to_have_count(2)
        page.get_by_label("Tìm video").fill("neverness")
        expect(page.locator(".vl-card")).to_have_count(1)
        page.get_by_label("Tìm video").fill("")
        page.get_by_label("Lọc loại video").select_option("original")
        expect(page.locator(".vl-card")).to_have_count(1)
        page.get_by_label("Lọc loại video").select_option("all")
        page.evaluate("navigator.clipboard.writeText('【Video chia sẻ】 https://b23.tv/abc。')")
        page.locator("#vl-links").focus()
        page.keyboard.press("Control+V")
        expect(page.get_by_text("Đang tải video", exact=True)).to_be_visible()
        expect(page.locator("#vl-links")).to_have_value("")
        assert requests[0]["urls"] == ["https://b23.tv/abc"]
        page.get_by_role("button", name="Hủy tải Video đang tải").click()
        expect(page.get_by_text("Đã hủy", exact=True)).to_be_visible()
        page.get_by_role("button", name="Thử lại", exact=True).click()
        expect(page.get_by_text("Đang tiếp tục lượt tải", exact=True)).to_be_visible()
        assert len(requests) == 1 and len(jobs) == 1 and retries == [jobs[0]["id"]]
        page.get_by_label("Tự tải khi dán link").uncheck()
        page.locator("#vl-links").fill("https://b23.tv/one\nhttps://b23.tv/two")
        page.get_by_role("button", name="Tải video", exact=True).click()
        expect(page.get_by_text("Đã nhận 2 link. Tiến độ tải hiển thị bên dưới.")).to_be_visible()
        assert len(requests[-1]["urls"]) == 2
        jobs[0].update(state="paused", phase="needs_cookies", error="Đã giữ file tải dở. Chọn lại cookies.")
        page.get_by_role("button", name="Làm mới thư viện").click()
        expect(page.get_by_role("button", name="Tiếp tục", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Mở video gốc", exact=True)).to_have_attribute("href", jobs[0]["url"])
        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name="Chọn cookies", exact=True).click()
        chooser.value.set_files({"name": "cookies.txt", "mimeType": "text/plain", "buffer": b"# Netscape HTTP Cookie File\n"})
        expect(page.get_by_label("File cookies đăng nhập")).to_be_visible()
        page.get_by_role("button", name="Tiếp tục", exact=True).click()
        expect(page.get_by_text("Đang tiếp tục lượt tải", exact=True)).to_be_visible()
        assert retries == [jobs[0]["id"], jobs[0]["id"]]
        jobs[0]["error"] = None
        page.get_by_role("checkbox", name="Chọn tất cả (2)", exact=True).check()
        page.screenshot(path=str(artifacts / "desktop.png"), full_page=True)
        for width in (320, 375, 414, 768):
            page.set_viewport_size({"width": width, "height": 1000})
            page.wait_for_timeout(100)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}px"
            for button in page.locator(".vl-button").all():
                if button.is_visible():
                    assert button.evaluate("el => el.scrollWidth <= el.clientWidth + 2"), f"Button overflow at {width}px"
            page.screenshot(path=str(artifacts / f"width-{width}.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        for job in jobs:
            job.update(state="succeeded", phase="complete", progress=100)
        added = {**videos[0], "id": "c" * 32, "title": "Video mới hoàn tất"}
        videos.append(added)
        page.get_by_role("button", name="Làm mới thư viện").click()
        expect(page.get_by_role("heading", name="Video mới hoàn tất")).to_be_visible()
        page.get_by_label("Tìm video").fill("không có kết quả")
        expect(page.get_by_role("heading", name="Không tìm thấy video")).to_be_visible()
        page.get_by_label("Tìm video").fill("")
        accept_dialogs = {"value": True}
        confirmations = []

        def confirm(dialog):
            confirmations.append(dialog.message)
            if accept_dialogs["value"]:
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", confirm)
        page.get_by_role("button", name="Xóa Video mới hoàn tất", exact=True).click()
        expect(page.locator(".vl-card")).to_have_count(2)

        # Select all includes unloaded cards and keeps type+id identity intact.
        template = videos[0].copy()
        videos.extend({**template, "id": f"{index:032x}", "title": f"Video hàng loạt {index}",
                       "platform": "Bilibili"} for index in range(25))
        videos.append({**template, "type": "subtitled", "title": "Bản phụ đề cùng ID"})
        page.get_by_role("button", name="Làm mới thư viện").click()
        expect(page.get_by_role("checkbox", name="Chọn tất cả (28)", exact=True)).to_be_visible()
        expect(page.locator(".vl-card")).to_have_count(24)
        page.get_by_role("checkbox", name="Chọn tất cả (28)", exact=True).check()
        expect(page.get_by_role("button", name="Xóa đã chọn (28)", exact=True)).to_be_enabled()
        page.get_by_role("checkbox", name="Chọn Khám phá thế giới Neverness to Everness", exact=True).uncheck()
        expect(page.get_by_role("button", name="Xóa đã chọn (27)", exact=True)).to_be_enabled()
        assert page.get_by_role("checkbox", name="Chọn tất cả (28)", exact=True).evaluate("el => el.indeterminate")

        # Changing filters clears selection; hidden uploads must never be deleted.
        page.get_by_label("Lọc nền tảng").select_option("Bilibili")
        expect(page.get_by_role("button", name="Xóa đã chọn (0)", exact=True)).to_be_disabled()
        page.get_by_role("checkbox", name="Chọn tất cả (27)", exact=True).check()
        before = len(deleted_requests)
        accept_dialogs["value"] = False
        page.get_by_role("button", name="Xóa đã chọn (27)", exact=True).click()
        assert len(deleted_requests) == before
        assert "27 video" in confirmations[-1]
        expect(page.get_by_role("button", name="Xóa đã chọn (27)", exact=True)).to_be_enabled()

        # Successful removals disappear; failed selection remains ready to retry.
        failed_deletes.add(("original", template["id"]))
        accept_dialogs["value"] = True
        page.get_by_role("button", name="Xóa đã chọn (27)", exact=True).click()
        expect(page.get_by_role("alert").filter(has_text="Đã xóa 26/27 video")).to_be_visible()
        expect(page.get_by_role("button", name="Xóa đã chọn (1)", exact=True)).to_be_enabled()
        assert len(deleted_requests) - before == 27
        assert ("subtitled", template["id"]) in deleted_requests
        assert ("original", "b" * 32) not in deleted_requests
        assert len(videos) == 2
        failed_deletes.clear()
        page.get_by_role("button", name="Xóa đã chọn (1)", exact=True).click()
        expect(page.get_by_text("Đã xóa 1 video.", exact=True)).to_be_visible()
        assert len(videos) == 1 and videos[0]["id"] == "b" * 32
        page.get_by_label("Lọc nền tảng").select_option("")
        expect(page.locator(".vl-card")).to_have_count(1)
        assert not errors, errors
        browser.close()
        print("PASS: downloads, single/bulk delete, select-all beyond pagination, partial selection, filter scoping, cancel confirmation, partial failure/retry, type+id identity, 4 responsive widths; no browser errors.")
        print(f"Screenshots: {artifacts}")


if __name__ == "__main__":
    main()
