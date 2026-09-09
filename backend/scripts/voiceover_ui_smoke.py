"""Browser smoke of the real voice panel/hook with an isolated in-memory API.

Run Vite on 5173 first. This does not generate speech or modify user projects.
"""
import json
import argparse
import io
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
HTML = r'''<!doctype html><html lang="vi"><meta charset="utf-8">
<style>html,body {height:auto!important;overflow:auto!important} .subtitle-studio-shell {position:relative!important;inset:auto!important;height:auto!important;overflow:visible!important;display:block!important}</style>
<div class="subtitle-studio-shell"><div id="root" style="width:100%;padding:12px"></div></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type;
window.__vite_plugin_react_preamble_installed__ = true;
const hookSource = await (await fetch('/src/voiceover/useVoiceover.ts')).text();
const reactPath = hookSource.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {VoicePanel} = await import('/src/voiceover/VoicePanel.tsx');
const {SubtitleTimeline} = await import('/src/subtitles/SubtitleTimeline.tsx');
const {PlaybackClockStore} = await import('/src/subtitles/playback-store.ts');
const {DEFAULT_FRAME_TIMING} = await import('/src/subtitles/types.ts');
const clock = new PlaybackClockStore(); clock.setDurationMs(10800000);
const {useVoiceover} = await import('/src/voiceover/useVoiceover.ts');
await import('/src/tokens.css');
await import('/src/styles.css');
await import('/src/subtitle-studio.css');
await import('/src/voiceover/voiceover.css');
const cues = [{id:'cue',start_ms:0,end_ms:5000,text:'Cô gái mở cửa.',revision:1}];
function App() { const [project, setProject] = React.useState('smoke');
 const [scale, setScale] = React.useState(50);
 const [selectedCueId, selectCue] = React.useState(null);
 const voice = useVoiceover(project, 'video', cues);
 return React.createElement(React.Fragment, null,
   React.createElement('button', {onClick: () => setProject('second')}, 'Đổi dự án thử'),
   React.createElement('button',{onClick:()=>setScale(scale===50?100:50)},'Đổi zoom thử'),
   React.createElement(SubtitleTimeline, {voice:{...voice,job:{state:'running',current_clip_id:voice.document?.clips[0]?.id,failed:[],completed_clip_ids:[]}},
     cues,selectedCueId,durationMs:10800000,frameTiming:DEFAULT_FRAME_TIMING,pixelsPerSecond:scale,clock,
     videoUrl:null,thumbnailCacheKey:null,hasOverlayTrack:false,snapEnabled:false,videoClips:[],selectedVideoClipId:null,
     onSelectCue:selectCue,onSeek:ms=>clock.setCurrentMs(ms),onTogglePlay:()=>{},onCueTimingCommit:()=>{},
     onSelectVideoClip:()=>{},onSplitVideo:()=>{},onDeleteVideoClip:()=>{}}),
   React.createElement(VoicePanel, {key: project, voice})); }
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
</script></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    args = parser.parse_args()
    document = None
    saves = []
    errors = []
    pending_profile = []
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setparams((1, 2, 48000, 0, 'NONE', 'none'))
        wav.writeframes(b'\0\0' * 48000 * 3)

    def api(route):
        nonlocal document
        request = route.request
        path = request.url.split('/voiceover', 1)[1]
        status, body = 200, {}
        if request.method == 'OPTIONS':
            pass
        elif path == '/status':
            body = dict(ready=True, installed=True, message='Bộ tạo giọng thử nghiệm', devices=[args.device], presets=[], model_revision=None)
        elif path in ('/profiles', '/projects/smoke/jobs'):
            if path == '/profiles' and request.method == 'POST':
                pending_profile.append(route)
                return
            body = []
        elif path == '/references':
            body = {'id': 'reference', 'duration_ms': 3000}
        elif path == '/references/reference':
            route.fulfill(body=audio.getvalue(), content_type='audio/wav', headers={'Access-Control-Allow-Origin': '*'})
            return
        elif path == '/projects/second/jobs':
            body = []
        elif path == '/projects/second':
            status, body = 404, {'detail': 'missing'}
        elif path == '/projects/smoke':
            if request.method == 'PUT':
                document = request.post_data_json
                document['revision'] += 1
                saves.append(json.loads(json.dumps(document)))
            if document is None:
                status, body = 404, {'detail': 'missing'}
            else:
                body = document
        route.fulfill(status=status, json=body, headers={
            'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*',
            'Access-Control-Allow-Methods': 'GET, PUT, POST, OPTIONS',
        })

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 420, 'height': 1000})
        page.on('pageerror', lambda error: (errors.append(str(error)), print(str(error), flush=True)))
        page.on('console', lambda msg: print(msg.text, flush=True) if msg.type == 'error' else None)
        page.route('**/api/v1/voiceover/**', api)
        page.route('**/__voice_smoke', lambda route: route.fulfill(content_type='text/html', body=HTML))
        page.goto('http://127.0.0.1:5173/__voice_smoke')
        page.get_by_role('button', name='Tạo đoạn từ phụ đề').click()
        block = page.locator('.voice-clip').first
        assert 'Đang tạo' in block.get_attribute('aria-label')
        voice_row = page.locator('.voice-track-row').bounding_box()
        subtitle_row = page.locator('.subtitle-track-row').bounding_box()
        assert voice_row['y'] + voice_row['height'] <= subtitle_row['y'] + 1
        assert abs(voice_row['x'] - subtitle_row['x']) < 1
        width = block.bounding_box()['width']
        page.get_by_role('button', name='Đổi zoom thử').click()
        assert abs(block.bounding_box()['width'] - width * 2) < 1
        page.get_by_role('button', name='Đổi zoom thử').click()
        page.locator('.subtitle-timeline-viewport').evaluate('(el)=>{el.scrollLeft=100}')
        page.wait_for_timeout(100)
        assert abs(page.locator('.voice-track-row').bounding_box()['x'] - page.locator('.subtitle-track-row').bounding_box()['x']) < 1
        page.locator('.subtitle-timeline-viewport').evaluate('(el)=>{el.scrollLeft=0}')
        block.click()
        block.press('ArrowRight')
        assert page.get_by_label('Độ lệch (ms)').input_value() == '10'
        bounds = block.bounding_box()
        x, y = bounds['x'] + 30, bounds['y'] + 20
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 20, y, steps=4)
        page.mouse.up()
        assert page.get_by_label('Độ lệch (ms)').input_value() == '410'
        page.get_by_role('button', name='Hoàn tác', exact=True).click()
        assert page.get_by_label('Độ lệch (ms)').input_value() == '10'
        assert page.get_by_label('Chạy trên').input_value() == args.device
        unavailable = 'cpu' if args.device == 'cuda' else 'cuda'
        assert page.get_by_label('Chạy trên').locator(f'option[value="{unavailable}"]').evaluate('(option) => option.disabled')
        page.get_by_text('Tạo giọng theo khoảng thời gian', exact=True).click()
        page.get_by_label('Từ giây', exact=True).fill('0')
        page.get_by_label('Đến giây', exact=True).fill('10')
        assert page.get_by_role('button', name='Tạo 1 đoạn trong khoảng').is_enabled()
        page.locator('fieldset textarea').fill('Lời đã chỉnh trong trình duyệt.')
        page.get_by_text('Đã lưu lời đọc', exact=True).wait_for(timeout=15000)
        assert saves[-1]['clips'][0]['spoken_text'] == 'Lời đã chỉnh trong trình duyệt.'
        assert saves[-1]['clips'][0]['offset_ms'] == 10
        document['clips'][0]['spoken_text'] = 'Lời sửa từ một cửa sổ khác.'
        document['mix']['original_gain'] = .6
        document['revision'] += 1
        page.locator('fieldset textarea').fill('Lời tôi muốn giữ sau đối chiếu.')
        page.get_by_role('button', name='Giữ phần sửa của tôi').wait_for(timeout=15000)
        page.get_by_role('button', name='Giữ phần sửa của tôi').click()
        page.get_by_text('Đã lưu lời đọc', exact=True).wait_for(timeout=15000)
        assert saves[-1]['clips'][0]['spoken_text'] == 'Lời tôi muốn giữ sau đối chiếu.'
        assert saves[-1]['mix']['original_gain'] == .6
        output = ROOT / 'artifacts/voiceover/ui'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'panel.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Panel overflows viewport'
        assert not errors, errors
        page.get_by_text('Tạo hồ sơ giọng từ mẫu', exact=True).click()
        page.locator('input[accept="audio/*"]').set_input_files({'name': 'sample.wav', 'mimeType': 'audio/wav', 'buffer': audio.getvalue()})
        page.get_by_role('button', name='Lưu và dùng mẫu giọng này').click()
        page.wait_for_timeout(100)
        assert pending_profile, 'Profile request was not intercepted'
        page.get_by_role('button', name='Đổi dự án thử').click()
        page.get_by_role('button', name='Tạo đoạn từ phụ đề').click()
        route = pending_profile.pop()
        route.fulfill(json=route.request.post_data_json, headers={'Access-Control-Allow-Origin': '*'})
        page.wait_for_timeout(300)
        assert page.get_by_role('combobox', name='Giọng đọc').input_value() == 'ngoc-huyen'
        assert not errors, errors
        print(json.dumps({'saved_revisions': len(saves), 'browser_errors': errors, 'screenshot': str(output / 'panel.png')}, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
