"""Browser contract check: real React panel against the real local proposal store.

Requires the frontend Vite server on 127.0.0.1:5173; Gemini responses are fixtures.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.gemini_review import (
    GeminiReviewStore,
    ReviewScope,
    validate_proposals,
)


def main():
    long_only = "--long" in sys.argv
    timing_only = "--timing" in sys.argv
    range_only = "--range" in sys.argv
    partial_failure = "--failed" in sys.argv
    root = Path(__file__).resolve().parents[2] / "artifacts" / "gemini-pipeline"
    document = {"schema_version": 2, "language": "vi", "timebase": "milliseconds", "timing_source": "gemini_estimate", "timing_precision_ms": 100,
                "segments": [{"id": "a", "start_ms": 1000, "end_ms": 2000, "text": "Sai tên", "needs_review": False, "revision": 0, "timing_source": "gemini_estimate", "timing_precision_ms": 100}]}
    document["segments"].append({**document["segments"][0], "id": "b", "start_ms": 3000, "end_ms": 4000, "text": "Câu thứ hai"})
    if long_only:
        document["segments"][0].update(end_ms=9000, text="Xin chào. Chúc một ngày tốt lành.")
        document["segments"][1].update(start_ms=10000, end_ms=11000)
        document["segments"].append({**document["segments"][0], "id": "locked", "start_ms": 12000, "end_ms": 20000, "locked": True})
    duration = 25000 if long_only else 5000
    if timing_only:
        duration = 30000
        document["segments"][0].update(start_ms=18000, end_ms=20000)
        document["segments"][1].update(start_ms=18300, end_ms=20300)
    before_text = document["segments"][0]["text"]
    after_text = "Xin chào." if long_only else "Trương Tam"
    if timing_only:
        after_text = before_text
    with tempfile.TemporaryDirectory(prefix="gemini-review-ui-") as temporary, tempfile.TemporaryDirectory(prefix="review-ui-", dir=Path(__file__).resolve().parents[2] / "frontend") as harness_directory, sync_playwright() as playwright:
        store = GeminiReviewStore(Path(temporary))
        state = {}
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1050, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        html = '''<html><meta charset="utf-8"><div id="root"></div>
<script type="module">import RefreshRuntime from '/@react-refresh';RefreshRuntime.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>(type)=>type;window.__vite_plugin_react_preamble_installed__=true;</script>
<script type="module">
import React from '/node_modules/.vite/deps/react.js';import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
import {GeminiReviewPanel} from '/src/subtitles/GeminiReviewPanel.tsx';
function Harness(){const [doc,setDoc]=React.useState(__DOC__);return React.createElement('main',{},
React.createElement('p',{id:'current'},doc.segments[0].text),
React.createElement('p',{id:'current-time'},String(doc.segments.find(c=>c.id==='a').start_ms)),
React.createElement('button',{id:'user-edit',onClick:()=>setDoc({...doc,segments:doc.segments.map(c=>({...c,text:'Người dùng đã sửa',revision:c.revision+1}))})},'Chỉnh thủ công'),
React.createElement(GeminiReviewPanel,{videoId:'aaaaaaaaaaaa',document:doc,durationMs:5000,selectedCueId:'a',initialModel:'gemini-3.6-flash',onDocument:setDoc,onSeek:ms=>window.lastSeek=ms}));}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(Harness));</script></html>'''.replace("__DOC__", json.dumps(document, ensure_ascii=False))
        if long_only:
            html = html.replace("durationMs:5000", "durationMs:25000")
        if timing_only:
            html = html.replace("durationMs:5000", "durationMs:30000")
        prefix, module_source = html.rsplit('<script type="module">', 1)
        module_source = module_source.split('</script>', 1)[0]
        module_source = module_source.replace("'/node_modules/.vite/deps/react.js'", "'react'").replace("'/node_modules/.vite/deps/react-dom_client.js'", "'react-dom/client'")
        module_path = Path(harness_directory) / "harness.tsx"
        module_path.write_text(module_source, encoding="utf-8")
        html = prefix + f'<script type="module" src="/{Path(harness_directory).name}/harness.tsx"></script></html>'
        page.route("**/__review_harness", lambda route: route.fulfill(content_type="text/html", body=html))
        def api_route(route):
            url = route.request.url
            body = route.request.post_data_json or {}
            try:
                if url.endswith("/review/gemini"):
                    expected_scope = ({"mode": "range", "start_ms": 1000, "end_ms": 4000, "combined": True} if range_only
                                      else {"mode": "all", "combined": True} if long_only or timing_only
                                      else {"mode": "selected", "cue_ids": ["a", "b"], "combined": True})
                    assert body["scope"] == expected_scope, body["scope"]
                    record = store.create(body["document"], ReviewScope.model_validate(body["scope"]), {"duration_ms": duration}, body["model"], body["video_id"])
                    proposal = {"operation": "edit", "issue": "terminology", "cue_ids": ["a"], "start_ms": 1000, "end_ms": 2000,
                                "after": [{"start_ms": 1000, "end_ms": 2000, "text": "Trương Tam"}], "reason": "Sửa tên theo video", "evidence": "张三 xuất hiện trên hình", "certainty": "high"}
                    if long_only:
                        proposal.update(operation="split", issue="readability", end_ms=9000, after=[
                            {"start_ms": 1000, "end_ms": 4000, "text": "Xin chào."},
                            {"start_ms": 4200, "end_ms": 9000, "text": "Chúc một ngày tốt lành."}])
                    if timing_only:
                        proposal.update(operation="retime", issue="timing", cue_ids=["a", "b"], start_ms=0, end_ms=30000, after=[
                            {"start_ms": 6000, "end_ms": 8000, "text": before_text},
                            {"start_ms": 9000, "end_ms": 11000, "text": "Câu thứ hai"}])
                    proposals = validate_proposals(json.dumps({"proposals": [proposal]}), snapshot=record["snapshot"], regions=record["regions"], allowed_ids={"a", "b"} if timing_only else {"a"}, media_start_ms=0, media_end_ms=duration, proposal_prefix="ui", combined=True)
                    state["id"] = record["id"]
                    result = store.update_run(record["id"], state="succeeded", clip_id="clip-00000", proposals=proposals)
                    state["job"] = {"id": "b" * 20, "kind": "review", "state": "succeeded", "progress": 100, "message": "Hoàn tất", "result": result}
                    if partial_failure:
                        store.update_run(record["id"], state="failed", warnings=[{"code": "review_clip_failed", "message": "Một vùng lỗi; đã giữ đề xuất hợp lệ."}])
                        state["job"].update(state="failed", result=None, error="Đã lưu 1/2 vùng; có thể tiếp tục.")
                    response = {"review": record, "job": {**state["job"], "state": "running", "result": None}}
                elif "/jobs/" in url:
                    response = state["job"]
                elif url.endswith("/apply"):
                    response = store.apply(state["id"], body["document"], body["proposal_ids"], skip=body.get("skip", False))
                elif url.endswith("/undo"):
                    response = store.undo(state["id"], body["document"])
                else:
                    response = store.public(store.load(state["id"]))
                route.fulfill(content_type="application/json", body=json.dumps(response, ensure_ascii=False), headers={"Access-Control-Allow-Origin": "*"})
            except ValueError as error:
                route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": str(error)}))
        page.route("**/api/v1/subtitles/**", api_route)
        page.goto("http://127.0.0.1:5173/__review_harness")
        page.wait_for_timeout(1000)
        assert not errors, errors
        assert page.get_by_role("heading", name="Kiểm tra sửa chữa bằng AI", exact=True).count() == 1
        assert page.get_by_label("Model kiểm tra", exact=True).count() == 1
        assert page.get_by_role("tab").count() == 0
        assert not state, "Opening the panel must not call Gemini"
        if range_only:
            page.get_by_role("combobox").select_option("range")
            page.get_by_label("Từ giây", exact=True).fill("1")
            page.get_by_label("Đến giây", exact=True).fill("4")
        elif long_only or timing_only:
            assert page.get_by_role("combobox").input_value() == "all"
        else:
            page.get_by_role("combobox").select_option("selected")
            page.get_by_role("checkbox").nth(0).uncheck()
            assert page.get_by_role("button", name="Sửa bằng AI", exact=True).is_disabled()
            page.get_by_role("checkbox").nth(0).check()
            page.get_by_role("checkbox").nth(1).check()
        page.get_by_role("button", name="Sửa bằng AI", exact=True).click()
        page.get_by_role("button", name="Áp dụng", exact=True).wait_for()
        if partial_failure:
            page.get_by_text("Một vùng lỗi; đã giữ đề xuất hợp lệ.", exact=True).wait_for()
            page.get_by_role("button", name="Tiếp tục kiểm tra", exact=True).wait_for()
        assert page.locator("#current").inner_text() == before_text
        page.get_by_role("button", name="Xem 0.00–30.00s" if timing_only else "Xem 1.00–9.00s" if long_only else "Xem 1.00–2.00s").click()
        assert page.evaluate("window.lastSeek") == (0 if timing_only else 1000)
        page.get_by_role("button", name="Áp dụng", exact=True).click()
        page.wait_for_function("text => document.querySelector('#current').textContent === text", arg=after_text)
        if timing_only:
            page.wait_for_function("document.querySelector('#current-time').textContent === '6000'")
        page.get_by_role("button", name="Hoàn tác lần áp dụng").click()
        page.wait_for_function("text => document.querySelector('#current').textContent === text", arg=before_text)
        if timing_only:
            page.wait_for_function("document.querySelector('#current-time').textContent === '18000'")
        page.locator("#user-edit").click()
        page.get_by_role("button", name="Áp dụng", exact=True).click()
        page.get_by_text("Xung đột — cần kiểm tra lại", exact=False).wait_for()
        assert page.locator("#current").inner_text() == "Người dùng đã sửa"
        page.screenshot(path=str(root / ("timing-review-ui.png" if timing_only else "long-cues-ui.png" if long_only else "review-ui.png")), full_page=True)
        assert not errors, errors
        browser.close()
        print(f"Review UI passed (long_only={long_only}, timing_only={timing_only}): scope selection, explicit start, media seek, proposal preview, apply, undo, concurrent-edit conflict.")


if __name__ == "__main__":
    main()
