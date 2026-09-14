"""Exercise the real voice panel/hook and playback guard with isolated API fixtures."""
import argparse
import io
import json
import tempfile
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:5173')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = root / 'artifacts/voiceover/sync-implementation'
    output.mkdir(parents=True, exist_ok=True)
    project = 'a' * 20
    clips = [{
        'id': name, 'source_cue_ids': [name], 'source_text': text, 'spoken_text': text,
        'start_ms': start, 'end_ms': end, 'offset_ms': offset, 'duration_ms': duration,
        'rate': 1, 'gain': 1, 'asset_id': name * 64, 'generation_hash': name * 64,
        'status': 'overflow' if name == 'a' else 'ready', 'error': None,
        'sync': {'timing_origin': 'legacy_unknown', 'timing_locked': True, 'text_locked': True,
                 'state': 'needs_review', 'issues': ['source_unverified', 'overlap']},
    } for name, text, start, end, offset, duration in [
        ('a', 'Câu đầu tiên.', 0, 1000, 100, 1000), ('b', 'Câu thứ hai.', 1000, 2000, 0, 600)]]
    doc = {'schema_version': 2, 'project_id': project, 'video_fingerprint': 'fixture', 'revision': 1,
           'profile': {'id': 'default', 'name': 'Default', 'preset': 'Default', 'reference_id': None,
                       'revision': 1, 'model_id': 'pnnbao-ump/VieNeu-TTS-v3-Turbo', 'model_revision': 'fixture'},
           'clips': clips, 'pronunciation': {},
           'mix': {'enabled': True, 'muted': False, 'gain': 1, 'original_gain': 1, 'mode': 'mix'}}
    with tempfile.TemporaryDirectory(prefix='voice-sync-ui-', dir=root / 'frontend') as temporary, sync_playwright() as pw:
        module = Path(temporary) / 'harness.tsx'
        module.write_text('''import React from 'react'; import {createRoot} from 'react-dom/client';
import {useVoiceover} from '/src/voiceover/useVoiceover.ts';
import {VoicePanel} from '/src/voiceover/VoicePanel.tsx';
import {VoicePlaybackEngine} from '/src/voiceover/playbackEngine.ts';
const initial = __DOC__;
const cues = initial.clips.map(c => ({id:c.id,text:c.source_text,start_ms:c.start_ms,end_ms:c.end_ms,
  timing_source:'manual',timing_precision_ms:1,needs_review:false,revision:1}));
function Harness(){ const [sourceCues,setSourceCues]=React.useState(cues);
  const voice=useVoiceover(initial.project_id,'fixture',sourceCues);
  window.changeSource=()=>setSourceCues(previous=>previous.map(c=>c.id==='a'?{...c,source_text:'changed original'}:c));
  window.voiceHarness=voice;
  return <><button onClick={()=>voice.setSelectedId('a')}>Select first</button><VoicePanel voice={voice}/></>; }
window.checkPlayback = async () => {
 const video=document.createElement('video'); let paused=false; video.currentTime=.1;
 Object.defineProperty(video,'paused',{get:()=>paused});
 video.pause=()=>{paused=true}; const errors=[];
 const engine=new VoicePlaybackEngine(video, message=>errors.push(message));
 engine.update(initial,1); await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 engine.dispose(); return {paused,errors};
};
createRoot(document.getElementById('root')).render(<Harness/>);
'''.replace('__DOC__', json.dumps(doc, ensure_ascii=False)), encoding='utf-8')
        html = f'''<meta charset="utf-8"><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh'; RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$=()=>{{}}; window.$RefreshSig$=()=>(type)=>type; window.__vite_plugin_react_preamble_installed__=true;
</script><script type="module" src="/{Path(temporary).name}/harness.tsx"></script>'''
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1100, 'height': 1000})
        errors, generation_requests, audits, applied_ids = [], [], [], []
        held_audit, canceled_audit, cancel_requests = False, False, []
        generation_enabled, voice_job, generation_cancels = False, None, []
        console_errors = []
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/__voice_sync_harness', lambda route: route.fulfill(content_type='text/html', body=html))

        def api(route):
            nonlocal doc, voice_job
            url, method = route.request.url, route.request.method
            if method == 'OPTIONS':
                route.fulfill(status=204, headers={'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': '*', 'Access-Control-Allow-Headers': '*'})
                return
            if url.endswith('/status'):
                value = {'ready': generation_enabled, 'installed': True, 'devices': ['cpu'], 'presets': [], 'message': 'Fixture'}
            elif url.endswith('/jobs') and method == 'POST':
                payload = route.request.post_data_json
                assert generation_enabled and len(payload['subtitle_document']['segments']) == 2
                assert payload['subtitle_document']['segments'][0]['source_text'] == 'changed original'
                generation_requests.append(payload)
                voice_job = {'id': 'b' * 20, 'project_id': project, 'state': 'running', 'phase': 'sync',
                    'message': 'Fixture automatic sync', 'completed': 2, 'total': 2, 'failed': [],
                    'elapsed_seconds': 1, 'eta_seconds': None, 'sync_progress': 35}
                value = voice_job
            elif '/jobs/' in url:
                if url.endswith('/cancel'):
                    generation_cancels.append(url)
                    voice_job['state'] = 'canceled'
                value = voice_job
            elif url.endswith('/profiles') or url.endswith('/jobs') and method == 'GET':
                value = []
            elif '/projects/' in url:
                if method == 'PUT':
                    doc = route.request.post_data_json
                    doc['revision'] += 1
                value = doc
            else:
                generation_requests.append((method, url))
                route.fulfill(status=400, content_type='application/json', body='{"detail":"Unexpected request"}')
                return
            route.fulfill(content_type='application/json', body=json.dumps(value),
                          headers={'Access-Control-Allow-Origin': '*'})

        page.route('**/api/v1/voiceover/**', api)

        def sync_api(route):
            nonlocal doc, canceled_audit
            if route.request.method == 'OPTIONS':
                route.fulfill(status=204, headers={'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': '*', 'Access-Control-Allow-Headers': '*'})
                return
            if route.request.url.endswith('/cancel'):
                canceled_audit = True
                cancel_requests.append(route.request.url)
                route.fulfill(content_type='application/json', body='{}', headers={'Access-Control-Allow-Origin': '*'})
                return
            if route.request.url.endswith('/apply'):
                payload = route.request.post_data_json
                assert payload['voice_revision'] == doc['revision'] and payload['clip_ids'] == ['a']
                updated = doc['clips'][0]
                updated.update(asset_id='f' * 64, offset_ms=50, rate=1, duration_ms=500, status='ready')
                cue = payload['document']['segments'][0]
                signature = json.dumps([updated[key] for key in ['id', 'source_cue_ids', 'start_ms', 'end_ms',
                    'offset_ms', 'spoken_text', 'source_text', 'asset_id', 'duration_ms']], ensure_ascii=False, separators=(',', ':'))
                source_signature = json.dumps([cue.get(key) for key in ['id', 'start_ms', 'end_ms', 'text',
                    'source_text', 'secondary_text', 'source_language', 'revision']], ensure_ascii=False, separators=(',', ':'))
                updated['sync'].update(state='aligned', issues=[], alignment={
                    'proof_id': 'e' * 64, 'original_asset_id': 'a' * 64, 'clip_signature': signature,
                    'source_signature': source_signature, 'source_start_ms': 150, 'source_end_ms': 550,
                    'allowed_end_ms': 550, 'speech_head_ms': 100, 'speech_tail_ms': 450, 'output_duration_ms': 500})
                doc['revision'] += 1
                applied_ids.extend(payload['clip_ids'])
                route.fulfill(content_type='application/json', body=json.dumps(doc),
                              headers={'Access-Control-Allow-Origin': '*'})
                return
            if route.request.url.endswith('/audio'):
                buffer = io.BytesIO()
                with wave.open(buffer, 'wb') as wav:
                    wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    wav.writeframes(b'\x00\x00' * 8000)
                route.fulfill(content_type='audio/wav', body=buffer.getvalue(),
                              headers={'Access-Control-Allow-Origin': '*'})
                return
            if route.request.method == 'POST':
                canceled_audit = False
                payload = route.request.post_data_json
                assert payload['clip_ids'] == ['a'] and payload['voice_revision'] == doc['revision']
                assert len(payload['document']['segments']) == 2
                audits.append(payload)
                value = {'audit_id': 'a' * 32, 'job': {'state': 'queued', 'progress': 0, 'message': 'Fixture queued'}}
            else:
                state = 'canceled' if canceled_audit else 'running' if held_audit else 'succeeded'
                value = {'audit_id': 'a' * 32, 'job': {'state': state, 'progress': 100, 'message': f'Fixture {state}'},
                    'audit': {'warnings': [], 'rows': [{'clip_id': 'a', 'completed': True, 'issues': ['source_unverified'],
                        'source_diagnostics': [{'matched_units': 5, 'expected_units': 5, 'minimum_confidence': .58,
                            'shared_word_boundary': False, 'observed_text': '孩子多吃点'}],
                        'processed': {'state': 'ready_for_review', 'issues': [],
                                      'audio': {'id': 'f' * 64, 'duration_ms': 500}},
                        'following_gap': {'start_ms': 2000, 'end_ms': 2100, 'classification': 'quiet_gap_candidate'}}]}}
            route.fulfill(content_type='application/json', body=json.dumps(value),
                          headers={'Access-Control-Allow-Origin': '*'})

        page.route('**/api/v1/subtitles/v2/voice-sync**', sync_api)
        page.goto(f'{args.base_url.rstrip("/")}/__voice_sync_harness')
        try:
            page.wait_for_function('window.voiceHarness?.document?.clips.length === 2')
        except Exception:
            print(json.dumps({'page_errors': errors, 'console_errors': console_errors,
                              'body': page.locator('body').inner_text()[:2000]}, ensure_ascii=True))
            raise
        page.get_by_text('Select first', exact=True).click()
        fit = page.get_by_role('button', name='Fit nhẹ, giữ mốc', exact=True)
        fit.click()
        assert page.evaluate('window.voiceHarness.document.clips[0].rate') == 1
        page.get_by_label('Giữ mốc và tốc độ đã chỉnh', exact=True).uncheck()
        fit.click()
        page.wait_for_function('window.voiceHarness.document.clips[0].rate === 1.12')
        assert page.evaluate('window.voiceHarness.document.clips.map(c=>c.offset_ms)') == [100, 0]
        page.get_by_label('Tốc độ', exact=True).fill('1.08')
        page.wait_for_function("window.voiceHarness.document.clips[0].sync.timing_origin === 'manual'")
        assert page.get_by_label('Giữ mốc và tốc độ đã chỉnh', exact=True).is_checked()
        page.evaluate("window.voiceHarness.history('undo')")
        page.wait_for_function('window.voiceHarness.document.clips[0].rate === 1.12')
        playback = page.evaluate('window.checkPlayback()')
        assert playback['paused'] and len(playback['errors']) == 1, playback
        assert 'Audio chồng nhau' in playback['errors'][0]
        assert page.get_by_role('button', name='Dịch mốc tránh đè', exact=True).count() == 0
        before_audit = page.evaluate('window.voiceHarness.document.clips')
        page.get_by_text('Kiểm tra độ khớp với lời nói nguồn', exact=True).click()
        page.get_by_role('button', name='Kiểm tra đoạn đang chọn', exact=True).click()
        page.get_by_text('ASR nghe được: 孩子多吃点', exact=True).wait_for()
        assert page.get_by_text('Ghép được 5/5 ký tự nguồn.', exact=True).is_visible()
        assert page.get_by_role('button', name='Kiểm tra 1 đoạn tiếp', exact=True).is_enabled()
        assert page.evaluate('window.voiceHarness.document.clips') == before_audit
        page.get_by_role('button', name='Áp dụng các đoạn đã đạt kiểm tra', exact=True).click()
        page.wait_for_function('window.voiceHarness.document.clips[0].offset_ms === 50')
        assert page.evaluate('window.voiceHarness.document.clips[0].rate') == 1
        assert page.evaluate('window.voiceHarness.document.clips[0].asset_id') == 'f' * 64
        assert page.evaluate('window.voiceHarness.document.clips[1].start_ms') == 1000
        page.evaluate("window.voiceHarness.history('undo')")
        page.wait_for_function('window.voiceHarness.document.clips[0].offset_ms === 100')
        assert page.evaluate('window.voiceHarness.document.clips') == before_audit
        assert applied_ids == ['a']
        assert len(audits) == 1
        page.get_by_role('button', name='Nghe WAV sau căn thời gian', exact=True).click()
        page.wait_for_function('document.querySelector("audio")?.readyState >= 1')
        assert page.evaluate('document.querySelector("audio").playbackRate') == 1
        assert page.evaluate('document.querySelector("audio").duration') == .5
        page.evaluate("window.voiceHarness.setSelectedId('b')")
        page.wait_for_function('document.querySelector("audio") === null')
        assert page.evaluate('window.voiceHarness.document.clips') == before_audit
        assert not errors, errors
        assert not generation_requests, generation_requests
        assert not cancel_requests, cancel_requests
        held_audit = True
        page.evaluate("window.voiceHarness.setSelectedId('a')")
        for change in ["window.voiceHarness.edit(doc=>({...doc,clips:doc.clips.map(c=>c.id==='a'?{...c,offset_ms:c.offset_ms+1}:c)}))",
                       'window.changeSource()']:
            page.get_by_role('button', name='Kiểm tra đoạn đang chọn', exact=True).click()
            page.get_by_text('Fixture running · 100%', exact=True).wait_for()
            page.evaluate(change)
            page.get_by_text('Fixture canceled · 100%', exact=True).wait_for()
        assert len(cancel_requests) == 2
        assert not errors, errors
        # Starting generation includes the actual source cues and stays active through sync.
        generation_enabled = True
        page.evaluate('window.voiceHarness.refreshStatus()')
        page.wait_for_function('window.voiceHarness.status.ready')
        page.evaluate("window.voiceHarness.run(['a'])")
        page.wait_for_function("window.voiceHarness.job?.phase === 'sync'")
        assert len(generation_requests) == 1 and not generation_cancels
        page.wait_for_function('document.querySelector(".voice-job progress")?.value === 35')
        page.evaluate("window.voiceHarness.edit(doc=>({...doc,clips:doc.clips.map(c=>c.id==='a'?{...c,offset_ms:c.offset_ms+1}:c)}))")
        page.wait_for_function("window.voiceHarness.job?.state === 'canceled'")
        assert len(generation_cancels) == 1
        assert not errors, errors
        page.screenshot(path=str(output / 'stage1-ui.png'), full_page=True)
        (output / 'stage1-ui.json').write_text(json.dumps({'passed': True,
            'checks': ['legacy lock', 'bounded fit preserves offset and next start', 'manual edit locks',
                       'undo', 'overlap pauses playback with explanation', 'source audit saves current revision',
                       'source diagnostics without changing timing', 'processed WAV loads at 1x and unmounts on selection',
                       'apply uses processed asset at 1x and supports undo',
                       'local timing/source edits cancel obsolete audit without saving',
                       'opening and auditing do not generate TTS',
                       'explicit generation sends source cues and shows sync progress',
                       'local voice edits cancel automatic sync'],
            'page_errors': errors}, indent=2), encoding='utf-8')
        browser.close()
        print('PASS: voice panel, hook, undo, playback guard and automatic sync lifecycle; API fixtures only.')


if __name__ == '__main__':
    main()
