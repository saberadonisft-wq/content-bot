"""Check actual audio graph/transport behavior with the playback benchmark fixture."""

import argparse
import io
import json
import math
import struct
import wave
from pathlib import Path
from urllib.parse import urlparse

from benchmark_voice_playback import HTML
from playwright.sync_api import sync_playwright

EXPOSE = r"""
const {VoicePlaybackEngine} = await import('/src/voiceover/playbackEngine.ts');
const originalUpdate = VoicePlaybackEngine.prototype.update;
VoicePlaybackEngine.prototype.update = function(...args) {window.engine=this; return originalUpdate.apply(this,args);};
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/refactor-playback/semantics.json"),
    )
    args = parser.parse_args()
    video_bytes = (args.output.parent / "fixture.mp4").read_bytes()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(math.sin(i * 2 * math.pi * 440 / 24_000) * 6000))
                for i in range(19_200)
            )
        )
    html = HTML.replace("const cueCount =", EXPOSE + "\nconst cueCount =").replace(
        "window.moveClip =", "window.changeDoc = fn => setDoc(fn); window.moveClip ="
    )
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def intercept(route):
            parsed = urlparse(route.request.url)
            if parsed.path == "/voice-semantics":
                route.fulfill(content_type="text/html", body=html)
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
        page.goto(args.url + "/voice-semantics?cues=500")
        page.wait_for_function(
            "window.engine?.cache.inflight===0 && window.engine?.activeDeck.clipId==='clip-0'"
        )

        def seek(seconds):
            page.evaluate(
                "value=>{document.querySelector('video').currentTime=value;}", seconds
            )
            page.wait_for_function(
                "value=>Math.abs(document.querySelector('video').currentTime-value)<0.01 && !document.querySelector('video').seeking && !window.engine.frame && window.engine.cache.inflight===0",
                arg=seconds,
            )

        seek(10.4)
        page.wait_for_function("engine.activeDeck.clipId==='clip-10'")
        assert abs(page.evaluate("engine.activeDeck.audio.currentTime") - 0.4) < 0.081
        assert (
            abs(page.evaluate("document.querySelector('video').volume") - 0.25) < 0.001
        )
        page.evaluate("window.edit(0.6)")
        page.wait_for_function("engine.sourceVolume===0.6 && !engine.frame")
        assert page.evaluate("metrics.contexts") == 1
        assert abs(page.evaluate("engine.master.gain.value") - 0.6) < 0.001
        page.evaluate("window.changeDoc(doc=>({...doc,mix:{...doc.mix,muted:true}}))")
        page.wait_for_function("engine.master.gain.value===0")
        assert (
            abs(page.evaluate("document.querySelector('video').volume") - 0.6) < 0.001
        )
        page.evaluate(
            "window.changeDoc(doc=>({...doc,mix:{...doc.mix,muted:false,mode:'voice'}}))"
        )
        page.wait_for_function("document.querySelector('video').volume===0")
        page.evaluate(
            "window.changeDoc(doc=>({...doc,mix:{...doc.mix,mode:'mix',original_gain:0.5}}))"
        )
        page.wait_for_function(
            "Math.abs(document.querySelector('video').volume-0.3)<0.001"
        )
        page.evaluate("document.querySelector('video').muted=true")
        page.wait_for_function("engine.activeDeck.audio.muted")
        page.evaluate(
            "document.querySelector('video').muted=false; document.querySelector('video').playbackRate=1.5"
        )
        page.wait_for_function(
            "engine.activeDeck.audio.playbackRate===1.5 && !engine.activeDeck.audio.muted"
        )
        page.evaluate(
            "window.changeDoc(doc=>({...doc,clips:doc.clips.map(c=>c.id==='clip-10'?{...c,asset_id:'replacement',rate:2}:c)}))"
        )
        seek(10.2)
        page.wait_for_function("engine.activeDeck.assetId==='replacement'")
        assert page.evaluate("engine.activeDeck.audio.playbackRate") == 3
        assert abs(page.evaluate("engine.activeDeck.audio.currentTime") - 0.4) < 0.081
        page.evaluate(
            "window.changeDoc(doc=>({...doc,mix:{...doc.mix,enabled:false}}))"
        )
        page.wait_for_function("engine.master.gain.value===0 && !engine.frame")
        assert page.evaluate("engine.activeDeck.audio.paused")
        assert (
            abs(page.evaluate("document.querySelector('video').volume") - 0.6) < 0.001
        )
        page.evaluate("window.changeDoc(doc=>({...doc,mix:{...doc.mix,enabled:true}}))")
        page.wait_for_function("engine.master.gain.value>0 && !engine.frame")
        assert page.evaluate("metrics.contexts") == 1
        # Observe actual samples downstream of the gain graph during steady audio.
        page.evaluate(
            "document.querySelector('video').playbackRate=1; window.changeDoc(doc=>({...doc,mix:{...doc.mix,mode:'voice',gain:1}}))"
        )
        seek(20)
        continuity = page.evaluate(r"""async () => {
          const video=document.querySelector('video'), analyser=engine.context.createAnalyser();
          analyser.fftSize=1024; engine.master.connect(analyser);
          const samples=new Float32Array(analyser.fftSize), result={checked:0,silent:0,maxDriftSeconds:0};
          await video.play(); const end=performance.now()+3200;
          while(performance.now()<end) {
            await new Promise(requestAnimationFrame);
            const phase=video.currentTime%1;
            if(phase<0.2||phase>0.6||video.seeking||video.paused) continue;
            analyser.getFloatTimeDomainData(samples);
            const rms=Math.sqrt(samples.reduce((sum,value)=>sum+value*value,0)/samples.length);
            result.checked++; if(rms<0.001) result.silent++;
            result.maxDriftSeconds=Math.max(result.maxDriftSeconds,Math.abs(engine.activeDeck.audio.currentTime-phase));
          }
          video.pause();engine.master.disconnect(analyser);analyser.disconnect();return result;
        }""")
        assert continuity["checked"] >= 40, continuity
        assert continuity["silent"] == 0, continuity
        assert continuity["maxDriftSeconds"] < 0.15, continuity
        # A fresh project must dispose the previous graph, abort fetches and clear URLs.
        page.evaluate(
            "window.previousEngine=engine; window.changeDoc(doc=>({...doc,project_id:'second',clips:[]}))"
        )
        page.wait_for_function(
            "metrics.contexts===2 && metrics.closed===1 && !engine.frame"
        )
        assert page.evaluate("previousEngine.cache.byteSize") == 0
        assert page.evaluate("previousEngine.context.state") == "closed"
        page.evaluate("window.root.unmount()")
        page.wait_for_function(
            "metrics.closed===2 && metrics.blobs===0 && metrics.activeFetches===0"
        )
        assert errors == [], errors
        assert page.evaluate("metrics.errors") == []
        report = {
            "browser": browser.version,
            "errors": errors,
            "steady_audio": continuity,
            "verified": [
                "seek_sync",
                "duck",
                "gain",
                "voice_mute",
                "video_mute",
                "voice_only",
                "mix_original_gain",
                "rate",
                "replace_asset_same_clip",
                "disable_enable",
                "steady_audio_samples",
                "project_switch",
                "cleanup",
            ],
        }
        browser.close()
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
