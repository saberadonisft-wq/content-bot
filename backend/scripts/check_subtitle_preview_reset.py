"""Browser regression: real workspace and libass worker must forget the previous video."""
import json
from io import BytesIO
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image
from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[2]
    output = root / 'artifacts/subtitle-preview-reset'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='preview-reset-', dir=root / 'frontend') as temporary:
        folder = Path(temporary)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi',
            '-i', 'color=c=black:s=640x360:d=3:r=10', '-an', '-c:v', 'libx264', '-threads', '1',
            '-pix_fmt', 'yuv420p', str(folder / 'blank.mp4')], check=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        (folder / 'harness.tsx').write_text('''import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {SubtitleWorkspace} from '/src/subtitles/SubtitleWorkspace.tsx';
import {DEFAULT_OPTIONS} from '/src/subtitles/studioConfig.ts';
import '/src/subtitle-studio.css';
const ass = `[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arimo,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,20,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,OLD VIDEO SUBTITLE
`;
const cue = {id:'old',text:'OLD VIDEO SUBTITLE',start_ms:0,end_ms:3000,
 timing_source:'manual',timing_precision_ms:1,needs_review:false,revision:1};
const noop=()=>{};
function Harness(){
 const [state,setState]=useState({source:'old',track:ass,cues:[cue]});
 window.setPreview=setState;
 window.oldPreview=()=>setState({source:'old',track:ass,cues:[cue]});
 window.newPreview=()=>setState({source:'new',track:null,cues:[]});
 window.clearPreview=()=>setState({source:'old',track:ass,cues:[]});
 return <SubtitleWorkspace originalVideoUrl={'/__preview_video?'+state.source} renderedVideoUrl={null}
  thumbnailCacheKey={null} media={null} liveAssContent={state.track} cues={state.cues}
  selectedCueId={null} options={DEFAULT_OPTIONS} overlayImage={null} overlayName=""
  overlayLayout={{x:50,y:50,width:20,opacity:1}} subtitleMasks={[]} selectedMaskId={null}
  videoClips={[{id:'clip',start_ms:0,end_ms:3000}]} selectedVideoClipId={null} canRestoreVideoClip={false}
  onDurationChange={noop} onSelectCue={noop} onUpdateCueText={noop} onCuePositionChange={noop}
  onActiveCueChange={noop} onCueTimingCommit={noop} onOptionsChange={noop} onOverlayLayoutChange={noop}
  onSelectMask={noop} onMaskChange={noop} onMaskDelete={noop} onSelectVideoClip={noop}
  onSplitVideo={noop} onDeleteVideoClip={noop} onRestoreVideoClip={noop}/>;
}
createRoot(document.getElementById('root')).render(<Harness/>);
''', encoding='utf-8')
        html = f'''<meta charset="utf-8"><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh'; RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$=()=>{{}}; window.$RefreshSig$=()=>(type)=>type; window.__vite_plugin_react_preamble_installed__=true;
</script><script type="module" src="/{folder.name}/harness.tsx"></script>'''
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport={'width': 1100, 'height': 850})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/__preview_reset', lambda route: route.fulfill(content_type='text/html', body=html))
            page.route('**/__preview_video?*', lambda route: route.fulfill(
                content_type='video/mp4', body=(folder / 'blank.mp4').read_bytes()))
            page.goto('http://127.0.0.1:5173/__preview_reset')
            visible = "getComputedStyle(document.querySelector('.preview-libass-host')).visibility === 'visible'"
            empty = "document.querySelectorAll('canvas.JASSUB').length === 0 && getComputedStyle(document.querySelector('.preview-libass-host')).visibility === 'hidden'"
            page.wait_for_function(visible, timeout=30000)
            page.wait_for_function("document.querySelector('video').readyState >= 2")
            page.locator('video').evaluate('async(video)=>{await video.play()}')
            page.wait_for_function("document.querySelector('video').currentTime > .4")
            page.locator('video').evaluate('(video)=>video.pause()')
            # Confirm real painted text, not only a ready flag or an empty canvas.
            pixels = Image.open(BytesIO(page.locator('.preview-libass-host').screenshot())).convert('RGB')
            assert max(high for _, high in pixels.getextrema()) > 180, 'No subtitle painted on black video'
            page.screenshot(path=str(output / 'before.png'))
            page.evaluate('window.newPreview()')
            page.wait_for_function(empty)
            page.screenshot(path=str(output / 'new-video.png'))
            # Deleting the last cue also hides an ASS response still in React state.
            page.evaluate('window.oldPreview()')
            page.wait_for_function(visible)
            page.evaluate('window.clearPreview()')
            page.wait_for_function(empty)
            # Changing video while the worker is initializing cannot revive its canvas.
            page.evaluate('window.oldPreview()')
            page.wait_for_function("document.querySelectorAll('canvas.JASSUB').length === 1")
            page.evaluate('window.newPreview()')
            page.wait_for_function(empty)
            page.wait_for_timeout(1000)
            assert page.evaluate(empty)
            assert not errors, errors
            (output / 'result.json').write_text(json.dumps({'passed': True, 'real_libass_worker': True,
                'checks': ['new video has no old canvas', 'last cue deletion clears preview',
                           'initializing worker cannot revive old track'], 'page_errors': errors}, indent=2), encoding='utf-8')
            browser.close()
    print('PASS: real libass preview resets for new video, empty cues and initialization race.')


if __name__ == '__main__':
    main()
