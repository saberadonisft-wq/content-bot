"""Offline Chromium smoke for the complete Subtitle Studio production path.

The fixture deliberately exercises the same UI handlers as a real project while
keeping OCR, ASR, translation, voice synthesis and video rendering deterministic.
It is intended to catch broken wiring, stale job polling and disabled controls;
media quality itself is covered by the backend tests and media fixtures.
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
from playwright.sync_api import expect, sync_playwright

HTML = r"""<!doctype html><html lang="vi"><meta charset="utf-8"><div id="root"></div>
<script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const source = await (await fetch('/src/SubtitleStudio.tsx')).text();
const reactPath = source.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)[1];
const {default: React} = await import(reactPath);
const {default: ReactDOM} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {SubtitleStudio} = await import('/src/SubtitleStudio.tsx');
await import('/src/tokens.css'); await import('/src/styles.css');
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(SubtitleStudio));
</script></html>"""

VIDEO = "f" * 16
PROJECT = {
    "schema_version": 2,
    "project_id": VIDEO,
    "video_fingerprint": VIDEO,
    "revision": 0,
    "profile": {
        "id": "ngoc-huyen",
        "name": "Ngọc Huyền",
        "revision": 1,
        "preset": "Ngọc Huyền",
        "reference_id": None,
        "model_id": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
        "model_revision": "fixture",
    },
    "clips": [],
    "pronunciation": {},
    "mix": {"enabled": True, "muted": False, "gain": 1, "original_gain": 0.25, "mode": "duck"},
}


def source_document(text="Hello"):
    return {
        "schema_version": 2,
        "timebase": "milliseconds",
        "language": "en",
        "timing_source": "asr",
        "timing_precision_ms": 100,
        "revision": 1,
        "run_id": "source-run",
        "document_role": "source",
        "segments": [{
            "id": "c1", "start_ms": 0, "end_ms": 1200, "text": text,
            "source_text": text, "source_language": "en", "timing_source": "asr",
            "timing_precision_ms": 100, "needs_review": False, "revision": 0,
        }],
    }


def translation_document(text="Xin chào"):
    return {
        "schema_version": 2,
        "timebase": "milliseconds",
        "language": "vi",
        "timing_source": "asr",
        "timing_precision_ms": 100,
        "revision": 2,
        "run_id": "translation-run",
        "document_role": "translation",
        "source_revision": 2,
        "source_run_id": "source-run",
        "translation_models": ["fixture-translate"],
        "segments": [{
            "id": "c1", "start_ms": 0, "end_ms": 1200, "text": text,
            "source_text": "Hello edited", "source_language": "en", "timing_source": "asr",
            "timing_precision_ms": 100, "needs_review": False, "revision": 1,
        }],
    }


def voice_document(clips=None, revision=1):
    value = json.loads(json.dumps(PROJECT))
    value["revision"] = revision
    value["clips"] = clips if clips is not None else []
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/subtitle-remediation/phase6/studio-pipeline.json"),
    )
    args = parser.parse_args()
    calls, errors, unexpected = [], [], []
    jobs = {}
    versions = []
    voice_state = {"exists": False, "document": None}
    voice_generation_count = 0
    translation_attempts = 0

    with tempfile.TemporaryDirectory(prefix="studio-pipeline-media-") as media_tmp:
        media_path = Path(media_tmp) / "fixture.mp4"
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-f", "lavfi",
                "-i", "color=c=black:s=640x360:r=25:d=2",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                str(media_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        media_bytes = media_path.read_bytes()

    media = {
        "fingerprint": VIDEO, "duration_ms": 2000, "width": 640, "height": 360,
        "rotation": 0, "has_audio": True, "is_vfr": False,
        "frame_rate_numerator": 25, "frame_rate_denominator": 1,
        "average_fps": 25, "frame_pts_ms": [], "source_start_ms": 0,
    }

    def job(kind, result=None, details=None, *, job_id=None, terminal_state="succeeded",
            error=None, ready_after_reads=1):
        job_id = job_id or f"{kind}-job"
        jobs[job_id] = {"reads": 0, "ready_after_reads": ready_after_reads, "value": {
            "id": job_id, "kind": kind, "state": "queued", "progress": 0,
            "phase": "queued", "message": f"Đang chạy {kind}", "cancel_requested": False,
            "details": details or {}, "result": result, "error": error,
            "terminal_state": terminal_state,
        }}
        return jobs[job_id]["value"]

    def route_api(route):
        nonlocal translation_attempts, voice_generation_count
        path = urlparse(route.request.url).path.split("/api/v1", 1)[-1]
        method = route.request.method
        data, status = {}, 200
        if method == "OPTIONS":
            route.fulfill(status=204, headers={
                "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "*",
            })
            return
        body = route.request.post_data_json if route.request.post_data else None
        calls.append({"method": method, "path": path, "body": body})

        if path.endswith("/metadata"):
            data = media
        elif path == "/subtitles/gemini/status":
            data = {"installed": True, "authenticated": False, "model": "fixture-model"}
        elif path == "/subtitles/gemini/models":
            data = {"models": [{"id": "fixture-model", "display_name": "Fixture"}], "selected_model": "fixture-model"}
        elif path == "/subtitles/v2/extract/asr/status":
            data = {"dependency_ready": True, "devices": [{"id": "cpu", "compute_types": ["int8"]}],
                    "models": [{"id": "small", "state": "ready"}], "allow_download": False,
                    "message": "ASR sẵn sàng"}
        elif path == "/subtitles/v2/extract/ocr" and method == "POST":
            data = job("ocr", {"document": source_document("OCR text"), "segment_count": 1,
                                "warnings": [], "region": body.get("region") if body else None},
                       job_id="a" * 20, ready_after_reads=3)
        elif path == "/subtitles/v2/extract/asr" and method == "POST":
            data = job("asr", {"document": source_document("Hello"), "segment_count": 1,
                                "warnings": [], "detected_language": "en", "device": "cpu", "compute_type": "int8"},
                       job_id="b" * 20)
        elif path == "/subtitles/v2/translate/gemini" and method == "POST":
            translation_attempts += 1
            if translation_attempts == 1:
                data = job("translation", None, {"resume_available": True, "translated_count": 1, "total_cues": 1},
                            job_id="c" * 20, terminal_state="failed",
                            error="Fixture Gemini timeout after checkpoint")
            else:
                data = job("translation", {"document": translation_document(), "segment_count": 1,
                                            "warnings": [], "translated_count": 1, "total_cues": 1,
                                            "version_id": "v-translation"}, job_id="d" * 20)
        elif path.startswith("/subtitles/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            record = jobs.get(job_id)
            if record is None:
                status, data = 404, {"detail": "unknown fixture job"}
            else:
                record["reads"] += 1
                if record["reads"] >= record.get("ready_after_reads", 1):
                    terminal_state = record["value"].pop("terminal_state", "succeeded")
                    record["value"]["state"] = terminal_state
                    record["value"]["progress"] = 100 if terminal_state == "succeeded" else 60
                    record["value"]["phase"] = "complete" if terminal_state == "succeeded" else "failed"
                    record["value"]["message"] = "Hoàn tất" if terminal_state == "succeeded" else "Tác vụ dịch thất bại sau checkpoint"
                data = record["value"]
        elif path == "/subtitles/v2/preview-ass":
            data = {"ass": "[Script Info]\nScriptType: v4.00+"}
        elif path == "/subtitles/v2/export/source-srt":
            data = {"srt": "1\n00:00:00,000 --> 00:00:01,200\nHello edited\n", "count": 1}
        elif path.endswith("/versions") and method == "POST":
            versions.append(body)
            data = {"id": f"version-{len(versions)}", "video_id": VIDEO,
                    "document": body["document"], "name": body.get("name", "fixture"),
                    "source": "manual", "created_at": "2026-09-15T00:00:00Z", "cue_count": 1}
        elif path.endswith("/versions"):
            data = {"versions": [], "total": 0, "offset": 0, "limit": 20}
        elif path == "/subtitles/v2/render" and method == "POST":
            data = job("render", {"subtitled_video_url": f"/api/v1/subtitles/video/{VIDEO}/rendered.mp4",
                                   "output_filename": "fixture-subtitled.mp4"}, job_id="e" * 20)
            data["kind"] = "render"
        elif path == "/voiceover/text-preview":
            data = {"text": body.get("text", "") if body else ""}
        elif path.startswith("/voiceover/assets/") and path.endswith("/peaks"):
            data = {"peaks": [[0.1, 0.25, 0.4, 0.2]]}
        elif path.startswith("/voiceover/assets/"):
            route.fulfill(status=200, body=b"RIFFfixture", content_type="audio/wav",
                          headers={"Access-Control-Allow-Origin": "*"})
            return
        elif path == "/voiceover/status":
            data = {"ready": True, "installed": True, "message": "Bộ tạo giọng sẵn sàng",
                    "devices": ["cpu"], "presets": [], "model_revision": "fixture",
                    "engines": [{"model_id": PROJECT["profile"]["model_id"], "model_revision": "fixture",
                                  "name": "Fixture", "ready": True, "devices": ["cpu"],
                                  "presets": [], "setup_command": "fixture", "message": "Sẵn sàng"}]}
        elif path == "/voiceover/profiles":
            data = [PROJECT["profile"]]
        elif path.startswith("/voiceover/projects/") and path.endswith("/jobs"):
            data = []
        elif path.startswith("/voiceover/projects/") and method == "GET":
            if not voice_state["exists"]:
                status, data = 404, {"detail": "voice project not found"}
            else:
                data = voice_state["document"]
        elif path.startswith("/voiceover/projects/") and method == "PUT":
            voice_state["exists"] = True
            value = body or voice_document()
            value["revision"] = int(value.get("revision", 0)) + 1
            voice_state["document"] = value
            data = value
        elif path == "/voiceover/jobs" and method == "POST":
            voice_generation_count += 1
            current = json.loads(json.dumps(voice_state["document"] or voice_document()))
            for clip in current["clips"]:
                clip["asset_id"] = f"asset-{clip['id']}"
                clip["status"] = "ready"
                clip["duration_ms"] = 800
            voice_state["document"] = current
            data = {"id": "voice-job", "project_id": VIDEO, "state": "queued", "message": "Đang tạo giọng",
                    "completed": 0, "total": len(current["clips"]), "failed": [], "elapsed_seconds": 0,
                    "eta_seconds": 1}
        elif path == "/voiceover/jobs/voice-job":
            data = {"id": "voice-job", "project_id": VIDEO, "state": "succeeded", "message": "Đã tạo giọng",
                    "completed": len((voice_state["document"] or {}).get("clips", [])),
                    "total": len((voice_state["document"] or {}).get("clips", [])), "failed": [],
                    "elapsed_seconds": 1, "eta_seconds": None}
        elif path.startswith("/voiceover/projects/") and path.endswith("/separations"):
            data = []
        elif path == f"/subtitles/video/{VIDEO}":
            route.fulfill(
                status=200,
                body=media_bytes,
                content_type="video/mp4",
                headers={"Accept-Ranges": "bytes", "Access-Control-Allow-Origin": "*"},
            )
            return
        elif path.startswith("/subtitles/video/"):
            status, data = 404, {"detail": "unknown fixture media path"}
        else:
            unexpected.append(f"{method} {path}")
            status, data = 404, {"detail": "unmocked fixture endpoint"}
        route.fulfill(status=status, json=data, headers={"Access-Control-Allow-Origin": "*"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/v1/**", route_api)
        page.route(args.url + "/studio-pipeline", lambda route: route.fulfill(content_type="text/html", body=HTML))
        page.add_init_script(
            "localStorage.setItem('content_bot_access_token','fixture');"
            "if (!localStorage.getItem('content-bot:subtitle-studio:v2')) "
            "localStorage.setItem('content-bot:subtitle-studio:v2', JSON.stringify({version:2,videoId:'"
            + VIDEO
            + "',projectName:'Fixture pipeline',mediaDurationMs:2000,cues:[],documentMeta:{revision:0},"
            + "extractionSettings:{mode:'ocr'},videoClips:[{id:'video-1',start_ms:0,end_ms:2000}]}));"
        )
        page.goto(args.url + "/studio-pipeline")
        page.wait_for_function("document.querySelector('.subtitle-studio-shell') !== null")
        pipeline = page.get_by_role("article", name="Tìm & dịch phụ đề", exact=True)
        expect(pipeline).to_be_visible()
        page.wait_for_function(
            "(() => { const video = document.querySelector('video'); "
            "return video && video.readyState >= 1 && video.videoWidth === 640 && video.videoHeight === 360; })()"
        )
        assert page.evaluate("document.querySelector('video').duration >= 1.9")
        checks = ["Chromium loads the generated MP4 and preserves its 16:9 dimensions"]

        # OCR then ASR are both wired to the same guarded job lifecycle.
        pipeline.get_by_role("button", name="Quét & tìm phụ đề", exact=True).click()
        page.wait_for_function("JSON.parse(localStorage.getItem('content-bot:subtitle-studio:v2') || '{}').pendingExtraction?.kind === 'ocr'")
        page.reload()
        page.wait_for_function("document.querySelector('.subtitle-studio-shell') !== null")
        page.wait_for_function("document.querySelector('.subtitle-job-progress.state-succeeded') !== null")
        page.get_by_text("Tùy chọn tìm phụ đề", exact=True).click()
        source_box = page.get_by_role("region", name="Chỉnh sửa phụ đề nguồn", exact=True).get_by_role("textbox", name="Nội dung đoạn 1", exact=True)
        expect(source_box).to_have_value("OCR text")
        checks.append("OCR pending job resumes after Studio reload")
        page.get_by_role("tab", name="ASR (Lời thoại)", exact=True).click()
        page.get_by_role("button", name="Nghe lời thoại (ASR)", exact=True).click()
        page.wait_for_function("document.querySelector('.subtitle-job-progress.state-succeeded') !== null")
        expect(source_box).to_have_value("Hello")
        checks.append("OCR extraction reaches succeeded")
        checks.append("ASR extraction reaches succeeded")

        # Edit and persist the source before asking Gemini to translate it.
        source_box.fill("Hello edited")
        page.get_by_role("button", name="Lưu nguồn", exact=True).click()
        expect(page.get_by_text("Đã lưu bản nguồn.", exact=True)).to_be_visible()
        checks.append("source edit is saved before translation")
        pipeline.get_by_role("button", name="Dịch tiếng Việt", exact=True).click()
        page.wait_for_function("document.querySelector('.subtitle-job-progress.state-failed') !== null")
        page.get_by_text("Thiết lập dịch & xuất SRT", exact=True).click()
        expect(page.get_by_text("Các nhóm đã dịch được giữ lại.", exact=False)).to_be_visible()
        checks.append("translation failure preserves a resumable checkpoint")
        pipeline.get_by_role("button", name="Tiếp tục dịch", exact=True).click()
        page.wait_for_function("document.querySelector('.subtitle-job-progress.state-succeeded') !== null")
        expect(page.get_by_text("Đã cập nhật bản dịch từ phụ đề nguồn.", exact=True)).to_be_visible()
        checks.append("translation resume applies to the working document")

        # Change the translated cue, then create/save a voice project from the same cue.
        boxes = page.get_by_role("textbox", name="Nội dung đoạn 1", exact=True)
        boxes.last.fill("Xin chào đã sửa")
        page.get_by_role("button", name="Giọng đọc", exact=True).click()
        page.get_by_role("button", name="Tạo đoạn từ phụ đề", exact=True).click()
        expect(page.get_by_text("Đoạn đang chọn", exact=False).last).to_be_visible(timeout=5000)
        page.get_by_role("button", name="Lưu lời đọc", exact=True).click()
        expect(page.get_by_text("Đã lưu lời đọc", exact=True)).to_be_visible(timeout=5000)
        page.get_by_role("button", name="Tạo phần còn thiếu", exact=True).click()
        page.wait_for_function("document.querySelector('.voice-panel') && document.body.innerText.includes('Đã tạo giọng')")
        checks.append("voice plan, save and synthesis job complete")

        # The toolbar must persist voice revision as part of the render request.
        page.get_by_role("button", name="Xuất video", exact=True).click()
        page.wait_for_function("document.body.innerText.includes('Tải video')")
        checks.append("render completes and exposes the download action")
        assert any(call["path"] == "/subtitles/v2/extract/ocr" for call in calls)
        assert any(call["path"] == "/subtitles/v2/extract/asr" for call in calls)
        assert any(call["path"] == "/subtitles/v2/translate/gemini" for call in calls)
        render_calls = [call for call in calls if call["path"] == "/subtitles/v2/render"]
        assert render_calls and render_calls[-1]["body"].get("voice_project_id") == VIDEO
        assert not errors, errors
        assert not unexpected, unexpected
        browser.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"passed": len(checks), "checks": checks,
                                       "versions_saved": len(versions),
                                       "translation_attempts": translation_attempts,
                                       "voice_generation_jobs": voice_generation_count,
                                       "page_errors": errors, "unexpected": unexpected},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": len(checks), "artifact": str(args.output)}))


if __name__ == "__main__":
    main()
