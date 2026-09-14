"""Browser key-list acceptance using a temporary encrypted vault, without Google calls."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.credential_vault import CredentialVault


def main():
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="keys-ui-", dir=root / "frontend") as harness, tempfile.TemporaryDirectory(prefix="keys-vault-") as temporary, sync_playwright() as playwright:
        vault = CredentialVault(Path(temporary))
        vault.setup_master_password("fixture-password")
        module = Path(harness) / "harness.tsx"
        module.write_text("import React from 'react';import{createRoot}from'react-dom/client';import '/src/tokens.css';import '/src/styles.css';import '/src/utility.css';import {GeminiKeySettings} from '/src/GeminiKeySettings.tsx';createRoot(document.getElementById('root')).render(<div style={{background:'#17212d',padding:24,maxWidth:700}}><GeminiKeySettings onChanged={async()=>{}}/></div>);", encoding="utf-8")
        html = '<html><meta charset="utf-8"><div id="root"></div><script type="module">import R from "/@react-refresh";R.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>(type)=>type;window.__vite_plugin_react_preamble_installed__=true;</script>' + f'<script type="module" src="/{Path(harness).name}/harness.tsx"></script></html>'
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/__keys_harness", lambda route: route.fulfill(content_type="text/html", body=html))
        def respond(route):
            path = urlparse(route.request.url).path
            body = route.request.post_data_json or {}
            if path.endswith('/keys') and route.request.method == 'POST':
                result = vault.edit_gemini_keys(add=body['keys'].splitlines())
            elif route.request.method == 'PATCH':
                result = vault.edit_gemini_keys(key_id=path.rsplit('/', 1)[-1], changes=body)
            elif route.request.method == 'DELETE':
                result = vault.edit_gemini_keys(key_id=path.rsplit('/', 1)[-1], delete=True)
            else:
                result = vault.gemini_key_status()
            encoded = json.dumps(result, ensure_ascii=False)
            assert "fixture-private-secret" not in encoded
            route.fulfill(content_type="application/json", body=encoded, headers={"Access-Control-Allow-Origin": "*"})
        page.route('**/api/v1/credentials/**', respond)
        page.goto('http://127.0.0.1:5173/__keys_harness')
        textbox = page.get_by_label('Thêm key, mỗi dòng một key')
        textbox.fill('\n'.join([f'fixture-private-secret-{i:03d}' for i in range(35)] + ['fixture-private-secret-000']))
        page.get_by_role('button', name='Thêm vào Vault', exact=True).click()
        page.get_by_text('Đã thêm 35 key; bỏ qua 1 key trùng.', exact=True).wait_for()
        assert textbox.input_value() == ''
        assert page.locator('li').count() == 10
        page.get_by_role('button', name='Sau', exact=True).click()
        assert 'Trang 2/4' in page.locator('body').inner_text()
        row = page.locator('li').first
        row.get_by_label('Tên key', exact=True).fill('Project thử nghiệm')
        assert page.get_by_label('Nhóm project (tùy chọn)', exact=True).count() == 0
        row.get_by_role('button', name='Lưu tên', exact=True).click()
        page.get_by_text('Đã cập nhật.', exact=True).wait_for()
        page.get_by_label('Tìm key', exact=True).fill('Project thử nghiệm')
        assert page.locator('li').count() == 1
        page.get_by_role('button', name='Tắt', exact=True).click()
        page.get_by_text('Đã tắt', exact=True).wait_for()
        assert sum(not item['enabled'] for item in vault.gemini_keys()) == 1
        page.get_by_role('button', name='Xóa', exact=True).click()
        page.wait_for_function("document.querySelectorAll('li').length === 0")
        assert len(vault.gemini_keys()) == 34
        assert 'fixture-private-secret' not in page.locator('body').inner_text()
        assert not errors, errors
        page.screenshot(path=str(root / 'artifacts/gemini-pipeline/key-settings-ui.png'), full_page=True)
        browser.close()
        print('Key UI passed: bulk import, dedupe, masked responses, pagination, search, rename, no group field, disable and delete against encrypted fixture vault.')


if __name__ == '__main__':
    main()
