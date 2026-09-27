"""Browser smoke for the real topic caption-cleaner call site."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

HTML = r"""<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="root"></div>
<script type="module">import RefreshRuntime from '/@react-refresh'; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script>
<script type="module">
const {default: React} = await import('/node_modules/.vite/deps/react.js');
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
import {TopicsWorkspace} from '/src/features/topics/TopicsWorkspace.tsx';
import '/src/tokens.css'; import '/src/styles.css'; import '/src/canva-home.css';
const item = {id: 1, source_id: 'douyin', canonical_url: 'https://example.test/post', title: 'Video thử nghiệm', body_snippet: 'Caption #review VX: demo_account 13800138000', author: 'tester', hashtags: ['review'], locale: 'vi', published_at: '2026-09-15T00:00:00Z', metrics: {}, relevance_score: 1, trend_score: 2, match_reasons: [], insights: {language: {code: 'vi', label: 'Tiếng Việt', confidence: 1, reason: ''}, sentiment: {label: 'neutral', score: 0, reasons: []}, topics: [], method: 'fixture'}};
const selected = {id: 1, name: 'Smoke', include_terms: [], exclude_terms: [], source_ids: [], channels: [], live_wall: {channel_ids: [], slots: {}}, enabled: false, interval_minutes: 60, max_items_per_source: 50, next_run_at: null, created_at: '2026-09-15T00:00:00Z', updated_at: '2026-09-15T00:00:00Z'};
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(TopicsWorkspace, {selected, keywords: [selected], items: [item], summary: null, summaryLoading: false, clusters: null, clustersLoading: false, clustersError: null, runs: [], sources: [], sourceFilter: '', setSourceFilter: ()=>{}, sessionFilter: '', setSessionFilter: ()=>{}, languageFilter: '', setLanguageFilter: ()=>{}, sentimentFilter: '', setSentimentFilter: ()=>{}, topicFilter: '', setTopicFilter: ()=>{}, loading: false, running: false, batch: null, canceling: false, onSelect: ()=>{}, onNew: ()=>{}, onRun: ()=>{}, onCancel: ()=>{}, onExport: '/api/v1/export.csv', onRefresh: ()=>{}, onEdit: ()=>{}, onDelete: ()=>{}}));
</script></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument("--output", type=Path, default=Path("artifacts/caption-cleaner/ui.png"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1200, "height": 1000},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_api(route):
            path = urlparse(route.request.url).path
            if path == "/api/v1/captions/clean":
                body = route.request.post_data_json["text"]
                route.fulfill(json={
                    "original_text": body,
                    "cleaned_text": "Video thử nghiệm\nCaption #review",
                    "hashtags": ["review"],
                    "safe_filename": "Video thu nghiem_Caption review",
                    "changed": True,
                })
                return
            if path == "/api/v1/items/1/caption":
                body = route.request.post_data_json
                route.fulfill(json={
                    "item_id": 1,
                    "caption_original": body.get("original_text", ""),
                    "caption_edited": body["edited_text"],
                    "caption_edited_at": "2026-09-15T00:00:00Z",
                })
                return
            route.fulfill(status=404, json={"detail": "fixture route unavailable"})

        page.route("**/api/v1/**", route_api)
        page.route("**/__caption_cleaner", lambda route: route.fulfill(content_type="text/html", body=HTML))
        page.goto(args.url + "/__caption_cleaner")
        trigger = page.get_by_role("button", name="Làm sạch caption: Video thử nghiệm", exact=True)
        expect(trigger).to_be_visible()
        trigger.click()
        editor = page.get_by_role("textbox", name="Caption bản nháp", exact=True)
        expect(editor).to_contain_text("VX: demo_account")
        page.get_by_role("button", name="Làm sạch", exact=True).click()
        expect(page.get_by_text("Video thử nghiệm", exact=False).last).to_be_visible()
        page.get_by_role("button", name="Lưu caption", exact=True).click()
        expect(page.get_by_text("Đã lưu vào bài viết", exact=False)).to_be_visible()
        for width in (360, 414, 768):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}px"
        page.screenshot(path=str(args.output), full_page=True)
        assert not errors, errors
        browser.close()
    print(f"PASS: caption cleaner preview/edit/apply at 3 responsive widths; screenshot: {args.output}")


if __name__ == "__main__":
    main()
