"""Check grouped-voice repair in the real panel/hook with an isolated API (Vite :5173)."""
import json

from playwright.sync_api import expect, sync_playwright

from voiceover_ui_smoke import HTML


def main():
    cues = [
        dict(id='a', start_ms=0, end_ms=3100, text='Nửa đêm ba canh,', revision=1),
        dict(id='b', start_ms=3200, end_ms=6000, text='cô nương vì sao lại ở đây?', revision=1),
        dict(id='c', start_ms=8800, end_ms=12500, text='Một câu riêng.', revision=1),
    ]
    html = HTML.replace(
        "const cues = [{id:'cue',start_ms:0,end_ms:5000,text:'Cô gái mở cửa.',revision:1}];",
        f'const cues = {json.dumps(cues, ensure_ascii=False)};',
    )
    text = ' '.join(cue['text'] for cue in cues[:2])
    grouped = dict(id='group', source_cue_ids=['a', 'b'], source_text=text, spoken_text=text,
                   start_ms=0, end_ms=6000, offset_ms=0, rate=1.08, gain=1,
                   asset_id='a' * 64, generation_hash='a' * 64, duration_ms=2800,
                   status='ready', error=None)
    single = dict(grouped, id='single', source_cue_ids=['c'], source_text=cues[2]['text'],
                  spoken_text=cues[2]['text'], start_ms=8800, end_ms=12500)
    document = dict(schema_version=1, project_id='smoke', video_fingerprint='video', revision=1,
                    profile=dict(id='test', name='Giọng thử', preset='test'), clips=[grouped, single],
                    pronunciation={}, mix=dict(enabled=True, muted=False, gain=1, original_gain=1, mode='mix'))
    errors, jobs = [], []

    def api(route):
        nonlocal document
        request = route.request
        path = request.url.split('/voiceover', 1)[1]
        body = {}
        if request.method == 'OPTIONS':
            pass
        elif path == '/status':
            body = dict(ready=True, installed=True, message='Bộ tạo giọng thử', devices=['cpu'], presets=[])
        elif path in ('/profiles', '/projects/smoke/jobs'):
            body = []
        elif path.endswith('/peaks'):
            body = {'peaks': [[0.2, 0.5, 0.3]]}
        elif path == '/projects/smoke':
            if request.method == 'PUT':
                document = request.post_data_json
                document['revision'] += 1
            body = document
        elif path == '/jobs':
            jobs.append(request.post_data_json)
            body = dict(id='job', project_id='smoke', state='succeeded', total=3, completed=3,
                        message='Đã kiểm tra yêu cầu tạo giọng', failed=[], eta_seconds=None)
        route.fulfill(json=body, headers={
            'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*',
            'Access-Control-Allow-Methods': 'GET, PUT, POST, OPTIONS',
        })

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/v1/voiceover/**', api)
        page.route('**/__voice_alignment', lambda route: route.fulfill(content_type='text/html', body=html))
        page.goto('http://127.0.0.1:5173/__voice_alignment')
        expect(page.locator('.voice-clip')).to_have_count(2)
        page.get_by_role('button', name='Tách theo từng phụ đề', exact=True).click()
        expect(page.locator('.voice-clip')).to_have_count(3)
        expect(page.get_by_text('Đã lưu lời đọc', exact=True)).to_be_visible(timeout=10000)
        assert [clip['start_ms'] for clip in document['clips']] == [0, 3200, 8800]
        assert [clip['source_cue_ids'] for clip in document['clips']] == [['a'], ['b'], ['c']]
        assert all(clip['asset_id'] is None for clip in document['clips'][:2])
        assert document['clips'][2] == single
        page.get_by_role('button', name='Hoàn tác', exact=True).click()
        expect(page.locator('.voice-clip')).to_have_count(2)
        expect(page.get_by_text('Đã lưu lời đọc', exact=True)).to_be_visible(timeout=10000)
        assert document['clips'][0] == grouped
        page.get_by_role('button', name='Làm lại', exact=True).click()
        expect(page.locator('.voice-clip')).to_have_count(3)
        expect(page.get_by_text('Đã lưu lời đọc', exact=True)).to_be_visible(timeout=10000)
        page.get_by_role('button', name='Tạo phần còn thiếu', exact=True).click()
        expect(page.get_by_text('3/3 đoạn · Đã kiểm tra yêu cầu tạo giọng', exact=True)).to_be_visible()
        assert jobs == [dict(project_id='smoke', device='cpu', clip_ids=None)]
        page.reload()
        expect(page.locator('.voice-clip')).to_have_count(3)
        expect(page.get_by_role('button', name='Tách theo từng phụ đề', exact=True)).to_have_count(0)
        assert not errors, errors
        browser.close()
    print(json.dumps(dict(passed=True, starts_ms=[0, 3200, 8800],
                          checks=['split', 'autosave', 'retain single audio', 'undo', 'redo', 'generate request', 'reload']), ensure_ascii=False))


if __name__ == '__main__':
    main()
