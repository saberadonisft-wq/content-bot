"""Real yt-dlp transfer against a local Range server, without external network."""
from __future__ import annotations

import io
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import imageio_ffmpeg
import pytest

from app.services import video_download_worker as worker


@pytest.mark.parametrize("status, fragment", [
    ("HTTP Error 412: Precondition Failed", "HTTP 412"),
    ("HTTP Error 403: Forbidden", "Link media có thể hết hạn"),
    ("HTTP Error 429: Too Many Requests", "HTTP 429"),
    ("Please sign in", "đăng nhập"),
])
def test_error_messages_distinguish_platform_blocks_without_leaking_secrets(status, fragment):
    message = worker.download_error_message(RuntimeError(f"{status} https://cdn.test?token=secret-cookie"))
    assert fragment in message
    assert "secret-cookie" not in message


def test_real_worker_resumes_partial_file_and_ignores_stale_formats(tmp_path, monkeypatch, capsys):
    source = tmp_path / "sample.mp4"
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True, timeout=20)
    content = source.read_bytes()
    assert len(content) > 4096
    ranges = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            range_header = self.headers.get("Range")
            ranges.append(range_header)
            start = int(range_header.split("=")[1].split("-")[0]) if range_header else 0
            self.send_response(206 if range_header else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(content) - start))
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{len(content) - 1}/{len(content)}")
            self.end_headers()
            try:
                self.wfile.write(content[start:])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    destination = tmp_path / "work"
    destination.mkdir()
    # Generic direct MP4 extractor emits format_id='mp4'.
    partial = destination / "video.sample.mp4.mp4.part"
    partial.write_bytes(content[:4096])
    # A previous format must neither be merged with the new one nor mistaken for output.
    (destination / "video.sample.old.mp4").write_bytes(b"stale other format")
    payload = {"url": f"http://127.0.0.1:{server.server_port}/sample.mp4",
               "directory": str(destination), "quality": "480"}
    # Only this loopback fixture is exempt from production SSRF checks.
    monkeypatch.setattr(worker, "guard_public_network", lambda: None)
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(payload) + "\n"))
    try:
        worker.main()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    complete = next(event for event in events if event["event"] == "complete")
    assert "bytes=4096-" in ranges, ranges
    assert (destination / complete["filename"]).read_bytes() == content
    assert not partial.exists()
