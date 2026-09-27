"""Profile the production editor with configurable synthetic subtitle and voice cues."""

import argparse
import base64
import io
import json
import math
import platform
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.subtitles import subtitles_to_ass

INSTRUMENT = r"""
window.profile={frames:[],longTasks:[],contexts:0,closed:0,assets:0,blobs:0,peakBlobs:0};
const Original=window.AudioContext;
window.AudioContext=class extends Original {constructor(...args){super(...args);profile.contexts++;} close(){profile.closed++;return super.close();}};
const create=URL.createObjectURL,revoke=URL.revokeObjectURL,sizes=new Map();
URL.createObjectURL=blob=>{const url=create(blob);sizes.set(url,blob.size);profile.blobs+=blob.size;profile.peakBlobs=Math.max(profile.peakBlobs,profile.blobs);return url;};
URL.revokeObjectURL=url=>{profile.blobs-=sizes.get(url)||0;sizes.delete(url);revoke(url);};
new PerformanceObserver(list=>profile.longTasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration})))).observe({entryTypes:['longtask']});
let previous;
function measure(time){if(previous){profile.frames.push(time-previous);if(profile.frames.length>4096)profile.frames.shift();}previous=time;requestAnimationFrame(measure);}
requestAnimationFrame(measure);
window.resetFrames=()=>{profile.frames=[];profile.longTasks=[];};
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5188")
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/refactor-playback/editor.json")
    )
    parser.add_argument(
        "--native-preview",
        action="store_true",
        help="Exercise the native fallback instead of the normal ASS renderer",
    )
    parser.add_argument("--memory-rounds", type=int, default=4)
    parser.add_argument("--seeks-per-round", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--counts",
        type=int,
        nargs="+",
        default=[500, 2000],
        help="Cue counts to profile; pass 5000 for the large-editor acceptance run.",
    )
    args = parser.parse_args()
    if min(args.memory_rounds, args.seeks_per_round, args.repetitions, *args.counts) < 1:
        parser.error("Memory rounds, seeks, and cue counts must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    duration_seconds = max(args.counts) + 100
    fixture = args.output.parent / f"editor-fixture-{duration_seconds}s.mp4"
    if not fixture.exists():
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color=c=gray:s=32x32:r=2:d={duration_seconds}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(fixture)],
            check=True, capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    video_bytes = fixture.read_bytes()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(math.sin(i * 2 * math.pi * 440 / 24_000) * 6000))
                for i in range(19_200)
            )
        )
    user = {
        "id": "fixture",
        "email": "fixture@example.com",
        "display_name": "Fixture",
        "role": "admin",
        "status": "approved",
        "auth_provider": "email",
    }
    video_id = "a" * 20
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--enable-precise-memory-info",
            ],
        )
        for run_index in range(len(args.counts) * args.repetitions):
            count = args.counts[run_index % len(args.counts)]
            cues = [
                {
                    "id": f"cue-{i}",
                    "start_ms": i * 1000,
                    "end_ms": i * 1000 + 800,
                    "text": f"Nội dung kiểm tra số {i}",
                    "timing_source": "manual",
                    "timing_precision_ms": 1,
                    "needs_review": False,
                    "revision": 1,
                }
                for i in range(count)
            ]
            draft = {
                "version": 2,
                "videoId": video_id,
                "projectName": f"Fixture {count}",
                "mediaDurationMs": duration_seconds * 1000,
                "cues": cues,
                "selectedCueId": "cue-0",
                "videoClips": [{"id": "video-1", "start_ms": 0, "end_ms": duration_seconds * 1000}],
            }
            voice = {
                "schema_version": 1,
                "project_id": video_id,
                "video_fingerprint": "fixture",
                "revision": 1,
                "profile": {
                    "id": "fixture",
                    "name": "Fixture",
                    "revision": 1,
                    "preset": "Fixture",
                    "reference_id": None,
                    "model_id": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
                    "model_revision": "fixture",
                },
                "pronunciation": {},
                "mix": {
                    "enabled": True,
                    "muted": False,
                    "gain": 1,
                    "original_gain": 1,
                    "mode": "duck",
                },
                "clips": [
                    {
                        "id": f"clip-{i}",
                        "source_cue_ids": [c["id"]],
                        "source_text": c["text"],
                        "spoken_text": c["text"],
                        "start_ms": c["start_ms"],
                        "end_ms": c["end_ms"],
                        "offset_ms": 0,
                        "rate": 1,
                        "gain": 1,
                        "asset_id": f"asset-{i}",
                        "generation_hash": "fixture",
                        "duration_ms": 800,
                        "status": "ready",
                        "error": None,
                    }
                    for i, c in enumerate(cues)
                ],
            }
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("console", lambda message: print(message.text, flush=True) if message.type in {"warning", "error"} else None)
            errors, requests = [], []
            page.on(
                "pageerror",
                lambda error, target=errors: (
                    target.append(str(error)),
                    print(str(error), flush=True),
                ),
            )
            page.add_init_script(
                INSTRUMENT
                + "\nlocalStorage.setItem('content_bot_access_token','fixture');localStorage.setItem('content_bot_user',"
                + json.dumps(json.dumps(user))
                + ");localStorage.setItem('content-bot:subtitle-studio:v2',"
                + json.dumps(json.dumps(draft))
                + ");"
            )

            def intercept(route, _request=None, voice_doc=voice, request_log=requests):
                parsed = urlparse(route.request.url)
                if parsed.netloc == urlparse(args.url).netloc:
                    route.continue_()
                    return
                request_log.append(parsed.path)
                headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Allow-Methods": "*",
                    "Access-Control-Expose-Headers": "*",
                }
                if parsed.path.endswith("/auth/me"):
                    data = user
                elif parsed.path.endswith("/versions"):
                    data = {"versions": [], "total": 0, "offset": 0, "limit": 20}
                elif parsed.path.endswith(
                    ("/sources", "/keywords", "/jobs", "/profiles", "/separations")
                ):
                    data = []
                elif parsed.path.endswith("/voiceover/status"):
                    data = {"ready": False, "devices": [], "presets": [], "engines": []}
                elif parsed.path.endswith("/voiceover/projects/" + video_id):
                    if route.request.method == "PUT":
                        body = route.request.post_data_json
                        voice_doc.update(body.get("document", body))
                    data = voice_doc
                elif parsed.path.endswith("/peaks"):
                    data = {"peaks": [[0.5] * 32]}
                elif "/voiceover/assets/" in parsed.path:
                    route.fulfill(
                        content_type="audio/wav", body=audio.getvalue(), headers=headers
                    )
                    return
                elif parsed.path.endswith("/metadata"):
                    data = {
                        "fingerprint": "fixture",
                        "file_size_bytes": len(video_bytes),
                        "duration_ms": duration_seconds * 1000,
                        "source_start_ms": 0,
                        "time_base_numerator": 1,
                        "time_base_denominator": 16384,
                        "frame_rate_numerator": 2,
                        "frame_rate_denominator": 1,
                        "average_fps": 2,
                        "frame_count": duration_seconds * 2,
                        "is_vfr": False,
                        "frame_pts_ms": [],
                        "frame_index_source": "average_fps",
                        "width": 32,
                        "height": 32,
                        "rotation": 0,
                        "video_codec": "h264",
                        "has_audio": False,
                    }
                elif parsed.path.endswith("/subtitles/video/" + video_id):
                    header = route.request.headers.get("range", "")
                    if header.startswith("bytes="):
                        bounds = header[6:].split("-", 1)
                        start, end = (
                            int(bounds[0]),
                            int(bounds[1]) if bounds[1] else len(video_bytes) - 1,
                        )
                        headers.update(
                            {
                                "Content-Range": f"bytes {start}-{end}/{len(video_bytes)}",
                                "Accept-Ranges": "bytes",
                            }
                        )
                        route.fulfill(
                            status=206,
                            content_type="video/mp4",
                            body=video_bytes[start : end + 1],
                            headers=headers,
                        )
                    else:
                        route.fulfill(
                            content_type="video/mp4", body=video_bytes, headers=headers
                        )
                    return
                elif parsed.path.endswith("/thumbnail-sprite"):
                    headers.update(
                        {
                            "X-Sprite-Frames": "1",
                            "X-Sprite-Frame-Width": "1",
                            "X-Sprite-Frame-Height": "1",
                        }
                    )
                    route.fulfill(
                        content_type="image/png",
                        body=base64.b64decode(
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
                        ),
                        headers=headers,
                    )
                    return
                elif parsed.path.endswith("/preview-ass"):
                    if args.native_preview:
                        route.fulfill(
                            status=503,
                            content_type="application/json",
                            body='{"detail":"fixture native preview"}',
                            headers=headers,
                        )
                        return
                    body = route.request.post_data_json
                    data = {
                        "ass": subtitles_to_ass(
                            body["document"]["segments"], body["options"]
                        ),
                        "play_res_x": 1280,
                        "play_res_y": 720,
                        "timing_precision_ms": 10,
                    }
                else:
                    data = {}
                route.fulfill(
                    content_type="application/json",
                    body=json.dumps(data),
                    headers=headers,
                )

            page.route("**/*", intercept)
            page.goto(args.url)
            start = time.perf_counter()
            page.get_by_role("button", name="Phụ đề Video", exact=True).click()
            page.wait_for_function(
                "document.querySelector('.voice-clip') && document.querySelector('video')?.readyState>=2 && profile.contexts>0"
            )
            ready_ms = (time.perf_counter() - start) * 1000
            page.wait_for_timeout(700)
            if not args.native_preview:
                page.wait_for_function(
                    "document.querySelector('.preview-subtitle-overlay')?.style.color==='transparent' && document.querySelector('canvas.JASSUB')"
                )
            renderer_ready_ms = (time.perf_counter() - start) * 1000
            mounted_rows = page.locator(".subtitle-cue-editor").count()
            assert 0 < mounted_rows <= 8, mounted_rows
            # Reach the actual final cue: a requested count alone is not evidence
            # that draft migration retained the whole large document.
            page.locator(".subtitle-virtual-list").evaluate("el => {el.scrollTop=el.scrollHeight;}")
            page.wait_for_function(
                "text => Array.from(document.querySelectorAll('.subtitle-cue-editor textarea')).some(el => el.value === text)",
                arg=cues[-1]["text"],
            )
            tail_rows = page.locator(".subtitle-cue-editor").count()
            assert 0 < tail_rows <= 8, tail_rows
            page.locator(".subtitle-virtual-list").evaluate("el => {el.scrollTop=0;}")
            page.wait_for_function("document.querySelector('.subtitle-cue-editor textarea')?.value==='Nội dung kiểm tra số 0'")
            page.evaluate("window.resetFrames()")
            page.get_by_role("button", name="Phát", exact=True).click()
            page.wait_for_timeout(3500)
            playback = page.evaluate("structuredClone(profile)")
            page.get_by_role("button", name="Tạm dừng", exact=True).click()
            before_edits = page.evaluate("profile.contexts")
            page.evaluate("window.resetFrames()")
            # Exercise timeline scrolling, subtitle list virtualization and a real pointer drag.
            block = page.locator(".voice-clip").first
            before_left = block.evaluate("el => parseFloat(el.style.left)")
            box = block.bounding_box()
            assert box, "Voice clip must be visible for the drag benchmark"
            page.mouse.move(
                box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            )
            page.mouse.down()
            page.mouse.move(
                box["x"] + box["width"] / 2 + 20,
                box["y"] + box["height"] / 2,
                steps=60,
            )
            page.mouse.up()
            page.wait_for_function(
                "before => parseFloat(document.querySelector('.voice-clip').style.left) > before",
                arg=before_left,
            )
            page.locator("summary").filter(has_text="Trộn âm thanh").click()
            slider = page.get_by_label("Âm lượng giọng", exact=True)
            slider.focus()
            initial_volume = float(slider.input_value())
            for _ in range(5):
                slider.press("ArrowLeft")
            assert float(slider.input_value()) < initial_volume
            page.wait_for_timeout(100)
            edits = page.evaluate("structuredClone(profile)")
            page.evaluate("window.resetFrames()")
            for i in range(10):
                page.evaluate(
                    "i=>{const list=document.querySelector('.subtitle-virtual-list');if(list){list.scrollTop=i*10000;list.dispatchEvent(new Event('scroll'));}document.querySelector('video').currentTime=i*37+0.2;}",
                    i,
                )
                page.wait_for_timeout(100)
            interactions = page.evaluate("structuredClone(profile)")
            cdp = page.context.new_cdp_session(page)
            heaps = []
            for round_index in range(args.memory_rounds):
                for i in range(args.seeks_per_round):
                    page.evaluate(
                        "value=>{document.querySelector('video').currentTime=value;}",
                        ((round_index * args.seeks_per_round + i) * 23) % count + 0.2,
                    )
                    page.wait_for_timeout(60)
                page.wait_for_timeout(300)
                cdp.send("HeapProfiler.collectGarbage")
                heaps.append(page.evaluate("performance.memory.usedJSHeapSize"))
            assert errors == [], errors
            assert page.evaluate("profile.contexts") == before_edits
            page.screenshot(path=str(args.output.parent / f"editor-{count}.png"))
            page.get_by_role("button", name="Về trang chính", exact=True).click()
            page.wait_for_function("profile.closed===profile.contexts")

            def summarize(data):
                frames = sorted(data["frames"])
                return {
                    "frame_count": len(frames),
                    "observed_ms": sum(frames),
                    "fps": 1000 / (sum(frames) / len(frames)),
                    "frame_interval_p95_ms": frames[int(len(frames) * 0.95)],
                    "long_tasks": data["longTasks"],
                }

            results.append(
                {
                    "cues": count,
                    "repetition": run_index // len(args.counts) + 1,
                    "ready_ms": ready_ms,
                    "renderer_ready_ms": renderer_ready_ms,
                    "mounted_subtitle_rows": mounted_rows,
                    "mounted_tail_rows": tail_rows,
                    "last_cue_verified": cues[-1]["id"],
                    "playback": summarize(playback),
                    "drag_and_volume": summarize(edits),
                    "interactions": summarize(interactions),
                    "heap_after_gc_bytes": heaps,
                    "peak_blob_bytes": page.evaluate("profile.peakBlobs"),
                    "contexts": page.evaluate("profile.contexts"),
                    "closed": page.evaluate("profile.closed"),
                    "requests": len(requests),
                    "errors": errors,
                }
            )
            page.close()
        report = {
            "browser": browser.version,
            "platform": platform.platform(),
            "fixture": str(fixture),
            "media_duration_seconds": duration_seconds,
            "url": args.url,
            "memory_rounds": args.memory_rounds,
            "seeks_per_round": args.seeks_per_round,
            "cue_counts": args.counts,
            "repetitions": args.repetitions,
            "mode": "editor; synthetic 32px/2fps video and voice; "
            + (
                "native preview fallback"
                if args.native_preview
                else "canonical backend ASS track and browser renderer"
            ),
            "results": results,
        }
        browser.close()
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
