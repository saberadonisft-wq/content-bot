"""Run the complete Studio component with isolated API fixtures in Chromium."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

HTML = r"""<!doctype html><html lang="vi"><meta charset="utf-8"><div id="root"></div>
<script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const source = await (await fetch('/src/SubtitleStudio.tsx')).text();
const reactPath = source.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {SubtitleStudio} = await import('/src/SubtitleStudio.tsx');
await import('/src/tokens.css'); await import('/src/styles.css');
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(SubtitleStudio));
</script></html>"""

DRAFT_KEY = "content-bot:subtitle-studio:v2"
VIDEO = "a" * 16


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/subtitle-remediation/phase1/studio-history.json"),
    )
    args = parser.parse_args()
    cue = {
        "id": "c1",
        "start_ms": 0,
        "end_ms": 1000,
        "text": "Bản dịch đầu",
        "source_text": "Hello",
        "timing_source": "ocr",
        "timing_precision_ms": 200,
        "needs_review": False,
        "revision": 0,
    }
    meta = {
        "schema_version": 2,
        "media_fingerprint": VIDEO,
        "timebase": "milliseconds",
        "language": "vi",
        "timing_source": "ocr",
        "timing_precision_ms": 200,
        "revision": 8,
        "run_id": "translation-first",
        "document_role": "translation",
        "source_revision": 2,
        "source_run_id": "source-first",
        "translation_models": ["fixture-model-first"],
    }
    second = {
        **meta,
        "revision": 1,
        "run_id": "translation-second",
        "source_revision": 5,
        "source_run_id": "source-second",
        "translation_models": ["fixture-model-second"],
        "segments": [{**cue, "text": "Bản dịch thứ hai"}],
    }
    version = {
        "id": "b" * 32,
        "video_id": VIDEO,
        "document": second,
        "name": "Bản dịch lưu để thử",
        "source": "translation",
        "created_at": "2026-09-15T00:00:00Z",
        "cue_count": 1,
        "media_fingerprint": VIDEO,
        "media_binding": "match",
    }
    draft = {
        "version": 2,
        "videoId": VIDEO,
        "cues": [cue],
        "documentMeta": meta,
        "mediaDurationMs": 2000,
        "projectName": "Kiểm thử lịch sử",
        "extractionSettings": {"mode": "ocr"},
        "translatedSourceRevision": 2,
    }
    calls, errors, unexpected, pending_imports = [], [], [], []
    scene_job = None
    scene_export_job = None
    reject_version_reads = False
    media = {
        "fingerprint": VIDEO,
        "duration_ms": 2000,
        "width": 640,
        "height": 360,
        "rotation": 0,
        "has_audio": False,
        "is_vfr": False,
        "frame_rate_numerator": 25,
        "frame_rate_denominator": 1,
        "average_fps": 25,
        "frame_pts_ms": [],
        "source_start_ms": 0,
    }

    def route_api(route):
        nonlocal scene_job, scene_export_job
        path = urlparse(route.request.url).path.split("/api/v1", 1)[-1]
        method = route.request.method
        data, status = {}, 200
        if method == "OPTIONS":
            route.fulfill(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Allow-Methods": "*",
                },
            )
            return
        if path.endswith("/metadata"):
            data = media
        elif path == "/subtitles/v2/scene-detect":
            scene_job = {
                "id": "c" * 20,
                "kind": "scene",
                "dedupe_key": "d" * 64,
                "state": "succeeded",
                "progress": 100,
                "phase": "complete",
                "message": "Đã tìm thấy 1 đoạn theo cảnh",
                "cancel_requested": False,
                "details": {},
                "result": {
                    "video_id": VIDEO,
                    "source_fingerprint": VIDEO,
                    "duration_s": 2,
                    "scene_cuts_s": [0, 2],
                    "chunks": [{"id": "scene-001", "start_s": 0, "end_s": 2,
                                "start_ms": 0, "end_ms": 2000, "duration_s": 2}],
                    "parameters": route.request.post_data_json,
                },
            }
            data = scene_job
        elif path == "/subtitles/v2/scene-export":
            scene_export_job = {
                "id": "e" * 20,
                "kind": "scene",
                "dedupe_key": "e" * 64,
                "state": "succeeded",
                "progress": 100,
                "phase": "complete",
                "message": "Đã xuất 1 Shorts",
                "cancel_requested": False,
                "details": {},
                "result": {
                    "video_id": VIDEO,
                    "source_fingerprint": VIDEO,
                    "manifest_url": f"/api/v1/subtitles/video/{VIDEO}/shorts/{VIDEO}_manifest.json",
                    "files": [{
                        "chunk_index": 1,
                        "start_ms": 0,
                        "end_ms": 2000,
                        "video_url": f"/api/v1/subtitles/video/{VIDEO}/shorts/{VIDEO}_short_01.mp4",
                        "srt_url": f"/api/v1/subtitles/video/{VIDEO}/shorts/{VIDEO}_short_01.srt",
                        "json_url": f"/api/v1/subtitles/video/{VIDEO}/shorts/{VIDEO}_short_01.json",
                    }],
                },
            }
            data = scene_export_job
        elif path.startswith("/subtitles/jobs/") and scene_job is not None:
            data = scene_export_job or scene_job
        elif path == "/voiceover/status":
            data = {"devices": ["cpu"], "ready": False, "profiles": []}
        elif path.endswith("/jobs"):
            data = []
        elif path.startswith("/voiceover/projects/"):
            status, data = 404, {"detail": "Fixture has no voice document"}
        elif path == "/subtitles/gemini/status":
            data = {"installed": True, "authenticated": False}
        elif path == "/subtitles/v2/extract/asr/status":
            data = {
                "dependency_ready": True,
                "devices": [{"id": "cpu", "compute_types": ["int8"]}],
                "models": [{"id": "small", "state": "ready"}],
                "allow_download": False,
            }
        elif path.endswith("/versions"):
            if method == "POST":
                calls.append(route.request.post_data_json)
                data = {**version, "document": route.request.post_data_json["document"]}
            else:
                data = {
                    "versions": [
                        {
                            key: value
                            for key, value in version.items()
                            if key != "document"
                        }
                    ],
                    "total": 1,
                    "offset": 0,
                    "limit": 20,
                }
        elif "/versions/" in path:
            if reject_version_reads:
                status, data = 409, {"detail": "Video nguồn đã thay đổi sau khi mở bản lưu."}
            else:
                data = version
        elif path == "/subtitles/v2/preview-ass":
            data = {"ass": ""}
        elif path == "/subtitles/v2/parse":
            pending_imports.append(route)
            return
        elif path.startswith("/subtitles/video/"):
            status, data = 404, {"detail": "No video decoding in history fixture"}
        else:
            unexpected.append(path)
            status, data = 404, {"detail": "Unmocked fixture endpoint"}
        route.fulfill(
            status=status, json=data, headers={"Access-Control-Allow-Origin": "*"}
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/v1/**", route_api)
        page.route(
            args.url + "/studio-history",
            lambda route: route.fulfill(content_type="text/html", body=HTML),
        )
        page.add_init_script(
            "if (!localStorage.getItem("
            + json.dumps(DRAFT_KEY)
            + ")) localStorage.setItem("
            + json.dumps(DRAFT_KEY)
            + ","
            + json.dumps(json.dumps(draft))
            + '); localStorage.setItem("content_bot_access_token","fixture");'
        )
        page.goto(args.url + "/studio-history")
        expect(page.get_by_role("textbox", name="Nội dung", exact=True)).to_have_value(
            "Bản dịch đầu"
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        for width in [1024, 1366, 1440]:
            page.set_viewport_size({"width": width, "height": 900})
            for mode in ["OCR (Trên hình)", "ASR (Lời thoại)", "Gemini Video"]:
                page.get_by_role("tab", name=mode, exact=True).click()
                page.get_by_role("tablist").scroll_into_view_if_needed()
                overflow = page.evaluate("""() => {
                  const panel = document.querySelector('.studio-panel-section.is-transcript').getBoundingClientRect();
                  return [...document.querySelectorAll('.subtitle-mode-tab, .subtitle-generation-panel select, .subtitle-generation-start')]
                    .filter(el => { const r=el.getBoundingClientRect(); return r.width && (r.left < panel.left-1 || r.right > panel.right+1 || el.scrollWidth > el.clientWidth+2); })
                    .map(el=>el.id || el.textContent.trim());
                }""")
                assert not overflow, (width, mode, overflow)
                for left, right in [
                    (".transport-mode-switch", ".transport-playback-controls"),
                    (".video-edit-toolbar-actions", ".video-edit-summary"),
                ]:
                    assert page.evaluate(
                        """([a,b]) => { const x=document.querySelector(a).getBoundingClientRect(), y=document.querySelector(b).getBoundingClientRect(); return !x.width || !y.width || x.right <= y.left + 1; }""",
                        [left, right],
                    ), (width, left, right)
            page.get_by_role("tab", name="OCR (Trên hình)", exact=True).click()
            page.get_by_role("tablist").scroll_into_view_if_needed()
            page.screenshot(
                path=str(args.output.with_name(f"studio-layout-{width}.png"))
            )
        page.set_viewport_size({"width": 1366, "height": 900})
        page.get_by_text("Phiên bản phụ đề", exact=True).click()
        page.get_by_role("button", name="Bản dịch lưu để thử").click()
        expect(page.get_by_role("button", name="Sử dụng bản này")).to_be_visible()
        reject_version_reads = True
        page.get_by_role("button", name="Sử dụng bản này").click()
        expect(page.get_by_text("Video nguồn đã thay đổi sau khi mở bản lưu.", exact=False)).to_be_visible()
        expect(page.get_by_role("textbox", name="Nội dung", exact=True)).to_have_value("Bản dịch đầu")
        reject_version_reads = False
        page.get_by_role("button", name="Sử dụng bản này").click()
        expect(page.get_by_role("textbox", name="Nội dung", exact=True)).to_have_value(
            "Bản dịch thứ hai"
        )
        assert calls[0]["document"]["run_id"] == "translation-first"
        assert calls[0]["preserve_source"] is True

        def persisted(text, model, source_revision):
            page.wait_for_function(
                "([key,text,model,revision]) => { const d=JSON.parse(localStorage.getItem(key)); return d?.cues[0]?.text===text && d.documentMeta.translation_models?.[0]===model && d.documentMeta.source_revision===revision; }",
                arg=[DRAFT_KEY, text, model, source_revision],
            )
            saved = page.evaluate(
                "(key)=>JSON.parse(localStorage.getItem(key))", DRAFT_KEY
            )
            assert saved["documentMeta"]["media_fingerprint"] == VIDEO
            return saved

        applied = persisted("Bản dịch thứ hai", "fixture-model-second", 5)
        page.get_by_role("button", name="Hoàn tác", exact=True).click()
        expect(page.get_by_role("textbox", name="Nội dung", exact=True)).to_have_value(
            "Bản dịch đầu"
        )
        undone = persisted("Bản dịch đầu", "fixture-model-first", 2)
        page.get_by_role("button", name="Làm lại", exact=True).click()
        redone = persisted("Bản dịch thứ hai", "fixture-model-second", 5)
        assert (
            applied["documentMeta"]["revision"]
            < undone["documentMeta"]["revision"]
            < redone["documentMeta"]["revision"]
        )
        page.reload()
        expect(page.get_by_role("textbox", name="Nội dung", exact=True)).to_have_value(
            "Bản dịch thứ hai"
        )
        restored = persisted("Bản dịch thứ hai", "fixture-model-second", 5)
        assert restored["documentMeta"]["source_run_id"] == "source-second"
        assert restored["documentMeta"]["timing_precision_ms"] == 200
        page.get_by_role("textbox", name="Kết quả Gemini, SRT hoặc VTT").fill(
            "1\n00:00:00,000 --> 00:00:01,000\nImported text"
        )
        page.get_by_role("button", name="Phân tích phụ đề", exact=True).click()
        page.wait_for_timeout(150)
        assert len(pending_imports) == 1
        page.get_by_role("textbox", name="Nội dung", exact=True).fill(
            "Chỉnh sửa trong lúc nhập"
        )
        imported = {
            key: value
            for key, value in second.items()
            if key
            not in {
                "source_revision",
                "source_run_id",
                "translation_models",
                "run_id",
                "document_role",
                "media_fingerprint",
            }
        }
        imported["segments"] = [{**cue, "text": "Imported text"}]
        pending_imports.pop().fulfill(
            json={"document": imported, "count": 1, "warnings": [], "srt": ""},
            headers={"Access-Control-Allow-Origin": "*"},
        )
        expect(page.get_by_role("textbox", name="Nội dung", exact=True)).to_have_value(
            "Chỉnh sửa trong lúc nhập"
        )
        expect(
            page.get_by_text(
                "Phụ đề hoặc nội dung nhập đã thay đổi trong lúc phân tích.",
                exact=False,
            )
        ).to_be_visible()
        page.get_by_role("button", name="Phân tích phụ đề", exact=True).click()
        page.wait_for_timeout(150)
        assert len(pending_imports) == 1
        pending_imports.pop().fulfill(
            json={"document": imported, "count": 1, "warnings": [], "srt": ""},
            headers={"Access-Control-Allow-Origin": "*"},
        )
        expect(page.get_by_role("textbox", name="Nội dung", exact=True)).to_have_value(
            "Imported text"
        )
        page.wait_for_function(
            '(key) => { const d=JSON.parse(localStorage.getItem(key)); return d.cues[0].text === "Imported text" && d.documentMeta.source_revision == null && !d.documentMeta.translation_models && !d.documentMeta.media_fingerprint; }',
            arg=DRAFT_KEY,
        )
        page.get_by_role("button", name="Hoàn tác", exact=True).click()
        persisted("Chỉnh sửa trong lúc nhập", "fixture-model-second", 5)
        video_tab = page.locator('.subtitle-tool-rail button[aria-label="Video"]')
        video_tab.click()
        expect(video_tab).to_have_attribute("aria-pressed", "true")
        page.get_by_role("button", name="Dò cảnh video", exact=True).click()
        expect(page.get_by_role("button", name="Dùng 1 chunk", exact=True)).to_be_visible()
        page.get_by_role("button", name="Dùng 1 chunk", exact=True).click()
        expect(page.get_by_text("Đã áp dụng 1 chunk", exact=False)).to_be_visible()
        page.get_by_role("button", name="Xuất 1 Shorts", exact=True).click()
        expect(page.get_by_text("Đã xuất 1 Shorts", exact=False).first).to_be_visible()
        assert not errors, errors
        assert not unexpected, unexpected
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "passed": 10,
                    "layout_checks": 9,
                    "layout_widths": [1024, 1366, 1440],
                    "checks": [
                        "apply-revalidates-video-and-keeps-editor-on-conflict",
                        "save-before-apply",
                        "atomic-document-undo",
                        "atomic-document-redo",
                        "reload-provenance",
                        "import-keeps-new-edits",
                        "import-clears-old-provenance",
                        "undo-import-restores-provenance",
                        "scene-detect-apply",
                        "scene-export",
                    ],
                    "api": "isolated fixtures",
                    "viewport": [1366, 900],
                    "errors": errors,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        page.screenshot(path=str(args.output.with_suffix(".png")))
        browser.close()
        print(json.dumps({"passed": 10, "artifact": str(args.output)}))


if __name__ == "__main__":
    main()
