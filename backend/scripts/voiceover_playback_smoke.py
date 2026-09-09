"""Exercise the actual playback hook with real HTML media in Chromium (Vite :5173)."""
import array
import json
import math
import sys
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.services.voiceover.mix import ffmpeg

HTML = r'''<!doctype html><html><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type;
window.__vite_plugin_react_preamble_installed__ = true;
const source = await (await fetch('/src/voiceover/useVoicePlayback.ts')).text();
const reactPath = source.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {useVoicePlayback} = await import('/src/voiceover/useVoicePlayback.ts');
const NativeAudio = window.Audio;
window.Audio = function(...args) { const audio = new NativeAudio(...args); window.voiceAudio = audio; return audio; };
const doc = {mix:{enabled:true,muted:false,gain:1,original_gain:1,mode:'duck'},clips:[
 {id:'one',asset_id:'tone',status:'ready',start_ms:1000,end_ms:5000,offset_ms:0,duration_ms:4000,rate:1,gain:1}
]};
window.playbackErrors = [];
const report = message => window.playbackErrors.push(message);
function App() {
 const [video, setVideo] = React.useState(null);
 useVoicePlayback(video, doc, true, 1, report);
 return React.createElement(React.Fragment,null,
   React.createElement('video',{ref:setVideo,src:'/test-video.mp4',controls:true,width:320}),
   React.createElement('button',{onClick:()=>video.play()},'Play'));
}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
</script></html>'''


def main():
    output = ROOT / 'artifacts/voiceover/playback'
    output.mkdir(parents=True, exist_ok=True)
    video_path, audio_path = output / 'video.mp4', output / 'voice.wav'
    if not video_path.exists():
        ffmpeg(['-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=30:d=8',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video_path)])
    with wave.open(str(audio_path), 'wb') as wav:
        wav.setparams((1, 2, 48000, 0, 'NONE', 'none'))
        samples = array.array('h', (round(3000 * math.sin(i * math.tau * 440 / 48000)) for i in range(48000 * 4)))
        wav.writeframes(samples.tobytes())
    errors, measurements = [], []
    def serve_video(route):
        data = video_path.read_bytes()
        requested = route.request.headers.get('range')
        headers = {'Accept-Ranges': 'bytes'}
        status = 200
        if requested:
            lower, upper = requested.removeprefix('bytes=').split('-')
            start = int(lower or 0)
            end = min(int(upper) if upper else len(data) - 1, len(data) - 1)
            headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
            data, status = data[start:end + 1], 206
        route.fulfill(status=status, content_type='video/mp4', body=data, headers=headers)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/__playback', lambda route: route.fulfill(content_type='text/html', body=HTML))
        page.route('**/test-video.mp4', serve_video)
        page.route('**/api/v1/voiceover/assets/tone', lambda route: route.fulfill(content_type='audio/wav', body=audio_path.read_bytes(), headers={'Access-Control-Allow-Origin': '*'}))
        page.goto('http://127.0.0.1:5173/__playback')
        page.get_by_role('button', name='Play', exact=True).click()
        page.wait_for_function('window.voiceAudio && !voiceAudio.paused && document.querySelector("video").currentTime > 1.3')
        def measure(label):
            value = page.evaluate('''() => { const v=document.querySelector('video'), a=voiceAudio;
              return {video:v.currentTime,audio:a.currentTime,drift_ms:Math.abs(a.currentTime-(v.currentTime-1))*1000,
                      paused:a.paused,rate:a.playbackRate,original_gain:v.volume}; }''')
            measurements.append({'case': label, **value})
            assert value['drift_ms'] <= 80, (label, value)
            assert not value['paused'], (label, value)
        measure('playing')
        page.evaluate('document.querySelector("video").pause()')
        page.wait_for_function('voiceAudio.paused')
        frozen = page.evaluate('voiceAudio.currentTime')
        page.wait_for_timeout(200)
        assert abs(page.evaluate('voiceAudio.currentTime') - frozen) < .02
        page.evaluate('document.querySelector("video").currentTime=2.5')
        page.wait_for_function('!document.querySelector("video").seeking')
        page.get_by_role('button', name='Play', exact=True).click()
        page.wait_for_timeout(400)
        measure('seek-and-resume')
        page.evaluate('document.querySelector("video").muted=true')
        page.wait_for_function('voiceAudio.muted')
        page.evaluate('document.querySelector("video").muted=false; document.querySelector("video").playbackRate=2')
        page.wait_for_timeout(300)
        measure('speed-two')
        assert measurements[-1]['rate'] == 2
        page.evaluate('document.querySelector("video").dispatchEvent(new Event("waiting"))')
        page.wait_for_function('voiceAudio.paused')
        page.evaluate('document.querySelector("video").dispatchEvent(new Event("playing"))')
        page.wait_for_function('!voiceAudio.paused')
        page.evaluate('document.querySelector("video").currentTime=6')
        page.wait_for_function('voiceAudio.paused && document.querySelector("video").volume === 1')
        measurements.append({'case': 'outside-narration', 'paused': True, 'original_gain': 1})
        assert not page.evaluate('window.playbackErrors'), page.evaluate('window.playbackErrors')
        assert not errors, errors
        (output / 'report.json').write_text(json.dumps({'measurements': measurements, 'page_errors': errors}, indent=2), encoding='utf-8')
        print(json.dumps(measurements), flush=True)
        browser.close()


if __name__ == '__main__':
    main()
