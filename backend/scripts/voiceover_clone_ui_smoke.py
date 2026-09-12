"""Exercise clone controls and interrupted-preview recovery with an isolated API."""
import io
import json
import wave
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from voiceover_ui_smoke import HTML


def main():
    document = None
    profiles, uploads, previews, errors = [], [], [], []
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setparams((1, 2, 48000, 0, 'NONE', 'none'))
        wav.writeframes(b'\x00\x01' * 48000 * 6)
    engines = [
        {'model_id': 'pnnbao-ump/VieNeu-TTS-v3-Turbo', 'model_revision': '8b7e9cffb4b41918cb638b9f62f0a751184d14a6',
             'name': 'VieNeu v3 Turbo', 'devices': ['cpu']},
        {'model_id': 'pnnbao-ump/VieNeu-TTS-v2-Turbo', 'model_revision': 'afe400abff18c00b52b246bb4d21f02a86855eb7',
             'name': 'VieNeu v2 Turbo', 'devices': ['cuda']},
    ]
    for engine in engines:
        engine.update(ready=True, presets=[], setup_command='setup', message='Sẵn sàng')

    def api(route):
        nonlocal document
        request = route.request
        url = urlsplit(request.url)
        path = url.path.split('/voiceover', 1)[1]
        status, body = 200, {}
        headers = {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*',
                   'Access-Control-Allow-Methods': 'GET, PUT, POST, OPTIONS'}
        if request.method == 'OPTIONS':
            pass
        elif path == '/status':
            body = {'ready': True, 'installed': True, 'message': 'Sẵn sàng', 'devices': ['cpu', 'cuda'],
                        'presets': [], 'engines': engines}
        elif path == '/profiles':
            if request.method == 'POST':
                body = request.post_data_json
                profiles.append(body)
            else:
                body = profiles
        elif path == '/references':
            uploads.append(parse_qs(url.query))
            body = {'id': 'a' * 64, 'duration_ms': 6000}
        elif path.startswith(('/references/', '/assets/')):
            route.fulfill(body=audio.getvalue(), content_type='audio/wav', headers=headers)
            return
        elif path.endswith('/jobs'):
            body = []
        elif path == '/projects/smoke':
            if request.method == 'PUT':
                document = request.post_data_json
                document['revision'] += 1
            body = document or {'detail': 'missing'}
            status = 200 if document else 404
        elif path == '/preview':
            previews.append(request.post_data_json)
            body = {'id': 'preview-job', 'project_id': 'preview', 'state': 'queued', 'message': 'Đang chờ'}
        elif path == '/jobs/preview-job':
            body = {'id': 'preview-job', 'project_id': 'preview', 'state': 'interrupted', 'message': 'Gián đoạn'}
        elif path == '/jobs/preview-job/resume':
            body = {'id': 'resumed', 'project_id': 'preview', 'state': 'queued', 'message': 'Đang tiếp tục'}
        elif path == '/jobs/resumed':
            body = {'id': 'resumed', 'project_id': 'preview', 'state': 'succeeded', 'message': 'Đã tạo xong'}
        elif path == '/projects/preview':
            body = {'clips': [{'asset_id': 'b' * 64}]}
        else:
            errors.append('Unexpected API path: ' + path)
        route.fulfill(status=status, json=body, headers=headers)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/v1/voiceover/**', api)
        page.route('**/__clone_smoke', lambda route: route.fulfill(content_type='text/html', body=HTML))
        page.goto('http://127.0.0.1:5173/__clone_smoke')
        page.get_by_role('button', name='Tạo đoạn từ phụ đề').click()
        page.get_by_text('Tạo hồ sơ giọng từ mẫu', exact=True).click()
        page.get_by_label('Bắt đầu tại giây').fill('4')
        page.get_by_label('Lấy số giây').fill('6')
        page.locator('input[accept="audio/*"]').set_input_files(
            {'name': 'sample.wav', 'mimeType': 'audio/wav', 'buffer': audio.getvalue()})
        page.get_by_role('button', name='Lưu và dùng mẫu giọng này').click()
        page.get_by_label('Xử lý mẫu giọng đang dùng').wait_for()
        assert uploads == [{'start_seconds': ['4'], 'duration_seconds': ['6']}]
        assert profiles[0]['denoise'] is False
        page.get_by_label('Xử lý mẫu giọng đang dùng').select_option('true')
        page.get_by_label('Engine tạo giọng').select_option(engines[1]['model_id'])
        assert page.get_by_label('Chạy trên').input_value() == 'cuda'
        page.get_by_role('button', name='Tạo mẫu nghe', exact=True).click()
        page.get_by_role('button', name='Tiếp tục mẫu nghe').click(timeout=10000)
        page.get_by_label('Nghe giọng đã tạo').wait_for(timeout=10000)
        assert previews[0]['profile']['model_id'] == engines[1]['model_id']
        assert previews[0]['profile']['reference_id'] == profiles[0]['reference_id']
        assert previews[0]['profile']['denoise'] is True
        assert previews[0]['device'] == 'cuda'
        page.locator('.voice-panel').screenshot(path='artifacts/voiceover/clone-controls.png')
        assert not errors, errors
        print(json.dumps({'clone_controls': 'passed', 'preview_resume': 'passed', 'browser_errors': errors}))
        browser.close()


if __name__ == '__main__':
    main()
