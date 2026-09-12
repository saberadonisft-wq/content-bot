"""Profile the real playback hook/audio elements with synthetic local media in Chromium.

This isolates transport cost; it is not a full editor or audible-quality benchmark.
Requires Vite and Playwright. No user media, credentials or inference runtime is used.
"""

import argparse
import io
import json
import math
import platform
import struct
import subprocess
import wave
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HTML = r"""<!doctype html><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const source = await (await fetch('/src/voiceover/useVoicePlayback.ts')).text();
const reactPath = source.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {useVoicePlayback} = await import('/src/voiceover/useVoicePlayback.ts');
const {DEFAULT_PROFILE} = await import('/src/voiceover/types.ts');
const cueCount = Number(new URLSearchParams(location.search).get('cues'));
window.metrics = {contexts:0, closed:0, frames:[], callbacks:{}, blobs:0, peakBlobs:0,
  activeFetches:0, peakFetches:0, assets:0, longTasks:[], errors:[]};
const m = window.metrics;
const OriginalContext = window.AudioContext;
window.AudioContext = class extends OriginalContext {
  constructor(...args) {super(...args); m.contexts++;}
  close() {m.closed++; return super.close();}
};
const raf = window.requestAnimationFrame;
window.requestAnimationFrame = callback => raf.call(window, timestamp => {
  const start = performance.now(); callback(timestamp);
  const name = callback.name || 'anonymous';
  const stats = m.callbacks[name] ||= {count:0, totalMs:0, maxMs:0};
  const duration = performance.now()-start; stats.count++; stats.totalMs+=duration; stats.maxMs=Math.max(stats.maxMs,duration);
});
const createURL = URL.createObjectURL, revokeURL = URL.revokeObjectURL, sizes = new Map();
URL.createObjectURL = blob => {const url=createURL(blob); sizes.set(url,blob.size); m.blobs+=blob.size; m.peakBlobs=Math.max(m.blobs,m.peakBlobs); return url;};
URL.revokeObjectURL = url => {m.blobs-=sizes.get(url)||0; sizes.delete(url); revokeURL(url);};
const originalFetch = window.fetch;
window.fetch = async (url, init) => {
  if (!String(url).includes('/voiceover/assets/')) return originalFetch(url,init);
  m.assets++; m.activeFetches++; m.peakFetches=Math.max(m.peakFetches,m.activeFetches);
  try {return await originalFetch(url,init);} finally {m.activeFetches--;}
};
new PerformanceObserver(list=>m.longTasks.push(...list.getEntries().map(e=>e.duration))).observe({entryTypes:['longtask']});
const clips = Array.from({length:cueCount},(_,i)=>({id:'clip-'+i,source_cue_ids:['cue-'+i],source_text:'fixture',spoken_text:'fixture',
  start_ms:i*1000,end_ms:i*1000+800,offset_ms:0,rate:1,gain:1,asset_id:'asset-'+i,duration_ms:800,status:'ready',error:null,generation_hash:'fixture'}));
const initial = {schema_version:1, project_id:'fixture', video_fingerprint:'fixture',revision:1,profile:DEFAULT_PROFILE,clips,pronunciation:{},
  mix:{enabled:true, muted:false, gain:1,original_gain:1,mode:'duck'}};
window.voiceAudio=null;
function App() {
  const [video,setVideo] = React.useState(null), [doc,setDoc] = React.useState(initial), [volume,setVolume] = React.useState(1);
  useVoicePlayback(video,doc,true,volume,message=>m.errors.push(message));
  window.edit = value => {setVolume(value);setDoc(current=>({...current,revision:current.revision+1,mix:{...current.mix,gain:value}}));};
  window.moveClip = offset => setDoc(current=>({...current,clips:current.clips.map((clip,i)=>i===1?{...clip,offset_ms:offset}:clip)}));
  return React.createElement('video',{ref:setVideo,id:'video',src:'/voice-benchmark.mp4',preload:'auto',playsInline:true,width:320,controls:true});
}
window.root=ReactDOM.createRoot(document.getElementById('root'));
window.root.render(React.createElement(App));
let previous;
function measure(timestamp) {if(previous)m.frames.push(timestamp-previous);previous=timestamp;raf.call(window,measure);}
raf.call(window,measure);
window.heap = () => performance.memory?.usedJSHeapSize ?? null;
</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    video = args.output.parent / "fixture.mp4"
    if not video.exists():
        from imageio_ffmpeg import get_ffmpeg_exe

        subprocess.run(
            [
                get_ffmpeg_exe(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=32x32:r=2",
                "-t",
                "2100",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(video),
            ],
            check=True,
        )
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(math.sin(i * 2 * math.pi * 440 / 24_000) * 600))
                for i in range(19_200)
            )
        )
    video_bytes = video.read_bytes()
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--enable-precise-memory-info",
            ],
        )
        for count in (500, 2000):
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error, target=errors: target.append(str(error)))

            def intercept(route):
                parsed = urlparse(route.request.url)
                if parsed.path == "/voice-benchmark":
                    route.fulfill(content_type="text/html", body=HTML)
                elif parsed.path == "/voice-benchmark.mp4":
                    header = route.request.headers.get("range", "")
                    if header.startswith("bytes="):
                        bounds = header[6:].split("-", 1)
                        start, end = (
                            int(bounds[0]),
                            int(bounds[1]) if bounds[1] else len(video_bytes) - 1,
                        )
                        route.fulfill(
                            status=206,
                            content_type="video/mp4",
                            body=video_bytes[start : end + 1],
                            headers={
                                "Content-Range": f"bytes {start}-{end}/{len(video_bytes)}",
                                "Accept-Ranges": "bytes",
                            },
                        )
                    else:
                        route.fulfill(content_type="video/mp4", body=video_bytes)
                elif "/voiceover/assets/" in parsed.path:
                    route.fulfill(
                        content_type="audio/wav",
                        body=audio.getvalue(),
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                elif parsed.netloc == urlparse(args.url).netloc:
                    route.continue_()
                else:
                    route.abort()

            page.route("**/*", intercept)
            page.goto(f"{args.url}/voice-benchmark?cues={count}")
            page.wait_for_function(
                "document.querySelector('video')?.readyState >= 2 && window.metrics?.contexts > 0"
            )
            page.wait_for_timeout(600)
            paused_start = page.evaluate("structuredClone(window.metrics.callbacks)")
            page.wait_for_timeout(600)
            paused_end = page.evaluate("structuredClone(window.metrics.callbacks)")
            heap_start = page.evaluate("window.heap()")
            page.evaluate("document.querySelector('video').play()")
            page.wait_for_timeout(2500)
            before_edits = page.evaluate("window.metrics.contexts")
            for index in range(10):
                page.evaluate("value=>window.edit(value)", 0.5 + index / 20)
                page.wait_for_timeout(80)
            contexts_after_edits = page.evaluate("window.metrics.contexts")
            page.evaluate(
                "document.querySelector('video').playbackRate=1.5; window.moveClip(100)"
            )
            page.evaluate(
                "target=>{document.querySelector('video').currentTime=target;}",
                count * 0.75 + 0.2,
            )
            page.wait_for_timeout(1000)
            page.evaluate("document.querySelector('video').pause()")
            for index in range(8):
                page.evaluate(
                    "target=>{document.querySelector('video').currentTime=target;}",
                    index * count / 10 + 0.2,
                )
                page.wait_for_timeout(100)
            page.wait_for_timeout(300)
            metrics = page.evaluate("structuredClone(window.metrics)")
            heap_end = page.evaluate("window.heap()")
            frames = sorted(metrics.pop("frames"))
            metrics["frame_interval_p95_ms"] = frames[int(len(frames) * 0.95)]
            metrics["observed_fps"] = 1000 / (sum(frames) / len(frames))
            page.evaluate("window.root.unmount()")
            page.wait_for_timeout(150)
            cleanup = page.evaluate(
                "({contexts:metrics.contexts,closed:metrics.closed,blobs:metrics.blobs,fetches:metrics.activeFetches})"
            )
            assert not errors, errors
            assert not metrics["errors"], metrics["errors"]
            assert (
                cleanup["contexts"] == cleanup["closed"]
                and cleanup["blobs"] == 0
                and cleanup["fetches"] == 0
            )
            results.append(
                {
                    "cues": count,
                    "metrics": metrics,
                    "paused_callbacks_start": paused_start,
                    "paused_callbacks_end": paused_end,
                    "contexts_added_by_volume_edits": contexts_after_edits
                    - before_edits,
                    "heap_start_bytes": heap_start,
                    "heap_end_bytes": heap_end,
                    "cleanup": cleanup,
                }
            )
            page.close()
        report = {
            "browser": browser.version,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "scope": "instrumented playback hook, real video/audio, synthetic 800 ms clips; excludes full editor and audible quality",
            "results": results,
        }
        browser.close()
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
