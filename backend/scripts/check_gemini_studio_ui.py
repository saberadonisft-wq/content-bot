"""Studio browser regression with fixture jobs and the real durable version store.

Requires Vite on 127.0.0.1:5173. No Gemini calls or user workspace data changes.
"""
from __future__ import annotations

import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas import SubtitleDocumentV2
from app.services.subtitle_versions import SubtitleVersionStore


def main():
    root = Path(__file__).resolve().parents[2]
    document = SubtitleDocumentV2.model_validate({"schema_version": 2, "run_id": "old-run", "segments": [
        {"id": "a", "start_ms": 1000, "end_ms": 3000, "text": "Bản đang chỉnh", "source_text": "你好", "source_language": "zh", "content_source": "audio", "needs_review": True}]}).model_dump(mode="json")
    media = {"duration_ms": 180000, "width": 1280, "height": 720, "has_audio": True, "frame_rate_numerator": 30, "frame_rate_denominator": 1, "average_fps": 30, "frame_pts_ms": [], "is_vfr": False, "fingerprint": "fixture"}
    draft = {"version": 2, "videoId": "a" * 12, "projectName": "Kiểm thử phiên bản", "mediaDurationMs": 180000, "cues": document["segments"], "selectedCueId": "a", "documentMeta": {"revision": 0, "run_id": "old-run"}}
    with tempfile.TemporaryDirectory(prefix="studio-ui-", dir=root / "frontend") as harness, tempfile.TemporaryDirectory(prefix="studio-versions-") as temporary, sync_playwright() as playwright:
        store = SubtitleVersionStore(Path(temporary))
        state = {"ready": False, "align_ready": False, "offline": False, "resuming": False, "submissions": 0}
        module = Path(harness) / "harness.tsx"
        module.write_text("import React from 'react';import{createRoot}from'react-dom/client';import '/src/tokens.css';import '/src/styles.css';import '/src/utility.css';import {SubtitleStudio} from '/src/SubtitleStudio.tsx';createRoot(document.getElementById('root')).render(<SubtitleStudio/>);", encoding="utf-8")
        html = '<html><meta charset="utf-8"><div id="root"></div><script type="module">import R from "/@react-refresh";R.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>(type)=>type;window.__vite_plugin_react_preamble_installed__=true;</script>' + f'<script type="module" src="/{Path(harness).name}/harness.tsx"></script></html>'
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("if (!localStorage.getItem('content-bot:subtitle-studio:v2')) localStorage.setItem('content-bot:subtitle-studio:v2', " + json.dumps(json.dumps(draft)) + ");")
        page.route("**/__studio_harness", lambda route: route.fulfill(content_type="text/html", body=html))
        def respond(route):
            url = urlparse(route.request.url)
            path = url.path
            body = route.request.post_data_json or {}
            status = 200
            if path.endswith('/gemini/status'):
                result = {"installed": True, "authenticated": True, "model": "gemini-3.6-flash"}
            elif path.endswith('/gemini/models'):
                result = {"models": [{"id": "gemini-3.6-flash", "display_name": "Gemini 3.6"}], "selected_model": "gemini-3.6-flash"}
            elif path.endswith('/metadata'):
                result = media
            elif '/versions' in path:
                if path.endswith('/versions') and route.request.method == 'POST':
                    result = store.save('a' * 12, body['document'], name=body['name'])
                elif path.endswith('/versions'):
                    result = store.list('a' * 12)
                else:
                    result = store.load('a' * 12, path.rsplit('/', 1)[-1])
            elif path.endswith('/generate/gemini'):
                state['submissions'] += 1
                assert body['regenerate'] is True
                assert body['current_document']['run_id'] == 'old-run'
                state['generation_request'] = body
                store.save('a' * 12, body['current_document'], name='Trước khi tạo lại')
                new = deepcopy(document)
                new['run_id'] = 'new-run'
                new['segments'][0]['text'] = 'Bản tạo mới'
                state['version'] = store.save('a' * 12, new, name='Gemini mới', source='generation')
                result = {"id": "b" * 20, "kind": "generation", "state": "running", "progress": 10, "message": "Đang chạy"}
            elif '/gemini/jobs/' in path:
                assert route.request.method == 'GET', 'Recovery must not require a resume request'
                result = {"id": "b" * 20, "kind": "generation", "state": "succeeded" if state['ready'] else "running", "progress": 100 if state['ready'] else 20, "message": "Đã hoàn tất" if state['ready'] else "Đang chạy"}
                result['details'] = {"completed": 1, "total": 8, "chunks": [
                    {"chunk_id": f"chunk-{index:05d}", "state": phase, "key_name": "Gemini 2", "model": "gemini-3.6-flash",
                     "message": "Đã hoàn tất 17/68 đoạn" if phase == 'completed' else "Chờ thử lại" if phase == 'retry_wait' else "Lỗi kiểm thử" if phase == 'failed' else ''}
                    for index, phase in enumerate(['completed', 'queued', 'preparing_video', 'uploading_video', 'gemini_analyzing', 'quota_wait', 'retry_wait', 'failed'], 1)]}
                if state['offline']:
                    status, result = 503, {"detail": "API fixture restarting"}
                elif state['resuming']:
                    result.update(state='queued', phase='resuming', progress=33, message='Đang tự tiếp tục các đoạn còn thiếu')
                if state['ready']:
                    result['result'] = {"document": state['version']['document'], "version_id": state['version']['id'], "srt": "", "warnings": [], "segment_count": 1, "chunk_count": 1, "actual_models": ["gemini-3.6-flash"]}
            elif path.endswith('/v2/align'):
                state['alignment_request'] = body
                new = deepcopy(body['document'])
                new['segments'][0]['end_ms'] = 3500
                state['alignment_version'] = store.save('a' * 12, new, name='Căn thời gian')
                result = {"id": "c" * 20, "kind": "alignment", "state": "running", "progress": 10, "message": "Đang căn"}
            elif '/subtitles/jobs/' in path:
                result = {"id": "c" * 20, "kind": "alignment", "state": "succeeded" if state['align_ready'] else "running", "progress": 100 if state['align_ready'] else 20, "message": "Hoàn tất"}
                if state['align_ready']:
                    result['result'] = {"document": state['alignment_version']['document'], "version_id": state['alignment_version']['id'], "engine": "energy", "aligned_cue_count": 1, "warnings": []}
            else:
                status, result = 404, {"detail": "No fixture for optional media/voice resource"}
            route.fulfill(status=status, content_type="application/json", body=json.dumps(result, ensure_ascii=False), headers={"Access-Control-Allow-Origin": "*"})
        page.route('**/api/v1/**', respond)
        page.goto('http://127.0.0.1:5173/__studio_harness')
        page.wait_for_timeout(1500)
        assert not errors, errors
        cue_editor = page.locator('.subtitle-cue-editor textarea').first
        cue_editor.wait_for()
        assert not errors, errors
        page.get_by_role('button', name='Tạo lại phụ đề', exact=True).click()
        cue_editor.fill('Người dùng sửa trong lúc tạo')
        page.locator('.gemini-chunk-details > summary').click()
        page.locator('.gemini-chunk-row').last.wait_for()
        assert page.locator('.gemini-chunk-row').count() == 8
        assert page.get_by_role('progressbar', name='chunk-00001: Hoàn tất', exact=True).get_attribute('aria-valuenow') == '100'
        assert page.get_by_role('progressbar', name='chunk-00002: Đang chờ', exact=True).get_attribute('aria-valuenow') == '0'
        assert page.get_by_role('progressbar', name='chunk-00005: Đang dịch', exact=True).get_attribute('aria-valuenow') is None
        assert page.get_by_role('img', name='chunk-00008: Lỗi', exact=True).count() == 1
        assert page.get_by_text('Đã hoàn tất 17/68 đoạn', exact=False).count() == 0
        assert page.locator('.gemini-chunk-list').evaluate('(el) => el.scrollHeight > el.clientHeight && el.scrollWidth <= el.clientWidth')
        page.screenshot(path=str(root / 'artifacts/gemini-pipeline/studio-chunk-progress.png'), full_page=True)
        state['offline'] = True
        page.get_by_text('API fixture restarting', exact=True).wait_for()
        state.update(offline=False, resuming=True)
        page.get_by_text('Đang tự tiếp tục các đoạn còn thiếu', exact=True).wait_for()
        page.get_by_text('API fixture restarting', exact=True).wait_for(state='hidden')
        assert page.get_by_role('button', name='Tiếp tục các đoạn còn thiếu', exact=True).count() == 0
        page.reload()
        page.get_by_text('Đang tự tiếp tục các đoạn còn thiếu', exact=True).wait_for()
        assert cue_editor.input_value() == 'Người dùng sửa trong lúc tạo'
        assert state['submissions'] == 1
        state['resuming'] = False
        state['ready'] = True
        page.get_by_text('Đã lưu bản mới trong Phiên bản phụ đề.', exact=False).wait_for()
        assert cue_editor.input_value() == 'Người dùng sửa trong lúc tạo'
        page.locator('.subtitle-versions-panel > summary').click()
        page.get_by_role('button', name='Gemini mới', exact=False).click()
        page.get_by_role('button', name='Sử dụng bản này', exact=True).click()
        page.wait_for_function("document.querySelector('.subtitle-cue-editor textarea').value === 'Bản tạo mới'")
        assert any(store.load('a' * 12, item['id'])['document']['segments'][0]['text'] == 'Người dùng sửa trong lúc tạo' for item in store.list('a' * 12)['versions'])
        page.get_by_label('Phạm vi căn').select_option('all')
        page.get_by_role('button', name='Căn lại thời gian', exact=True).click()
        cue_editor.fill('Người dùng sửa trong lúc căn')
        state['align_ready'] = True
        page.get_by_text('Đã lưu kết quả căn trong Phiên bản phụ đề.', exact=False).wait_for()
        assert cue_editor.input_value() == 'Người dùng sửa trong lúc căn'
        assert state['alignment_request']['document']['segments'][0]['source_text'] == '你好'
        page.get_by_role('button', name='Khóa AI', exact=True).click()
        page.get_by_role('button', name='Mở khóa AI', exact=True).wait_for()
        page.screenshot(path=str(root / 'artifacts/gemini-pipeline/studio-versions-ui.png'), full_page=True)
        assert not errors, errors
        browser.close()
        print('Studio UI passed: automatic recovery after API outage and page reload without resume POST, regeneration preserves edits, version selection saves draft, alignment preserves concurrent edits, source metadata and lock control.')


if __name__ == '__main__':
    main()
