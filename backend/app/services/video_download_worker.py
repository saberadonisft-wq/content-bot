"""Isolated yt-dlp process. Stdout is a small JSON event stream for the manager."""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from app.crawlers import SOURCE_REGISTRY
from app.crawlers.runtime import ProfileLock, ProfileNamespace


def emit(event: str, **values) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)


def download_error_message(error: Exception) -> str:
    # Publish neither signed URLs nor cookie values from extractor diagnostics.
    message = str(error).casefold()
    if "profile" in message and any(
        marker in message for marker in ("owned", "lock", "pending deletion")
    ):
        return "Profile đăng nhập đang được browser hoặc lượt tải khác sử dụng. Đóng phiên đó rồi thử lại."
    if "412" in message or "precondition failed" in message:
        return "Nền tảng chặn yêu cầu tải tự động (HTTP 412). Mở video gốc trong trình duyệt, hoàn tất xác minh nếu có, rồi chọn cookies từ phiên đó và thử lại. Cookies không đảm bảo gỡ được giới hạn mạng/IP."
    if "429" in message or "too many requests" in message:
        return "Nền tảng giới hạn số yêu cầu (HTTP 429). Chờ một lúc rồi thử lại lượt tải này."
    if "403" in message:
        return "Nguồn từ chối truy cập (HTTP 403). Link media có thể hết hạn hoặc quyền truy cập bị giới hạn. Bấm Thử lại để lấy link mới; nếu video cần đăng nhập, chọn cookies trước."
    if any(word in message for word in ("login", "cookie", "sign in", "premium", "members")):
        return "Nền tảng yêu cầu đăng nhập hoặc giới hạn truy cập. Chọn file cookies còn hiệu lực rồi thử lại."
    if any(word in message for word in ("unsupported url", "no video formats", "video cụ thể")):
        return "Chưa tải được link này. Hãy dùng link một video công khai trên nền tảng được hỗ trợ."
    if "timed out" in message or "timeout" in message:
        return "Kết nối tới nền tảng quá thời gian chờ. Vui lòng thử lại."
    return "Không tải được video. Kiểm tra link, quyền truy cập hoặc cập nhật yt-dlp rồi thử lại."


def guard_public_network() -> None:
    # Runs only inside this worker, never changes the application's networking.
    original = socket.getaddrinfo

    def public_addresses(*args, **kwargs):
        addresses = original(*args, **kwargs)
        if not addresses or any(
            not ipaddress.ip_address(entry[4][0].split("%", 1)[0]).is_global
            for entry in addresses
        ):
            raise OSError("Không cho phép tải từ địa chỉ mạng nội bộ.")
        return addresses

    socket.getaddrinfo = public_addresses


@contextmanager
def work_lock(directory: Path):
    """An orphaned worker must finish/exit before a replacement uses its files."""
    with (directory / ".worker.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.25)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _connection_profile(payload: dict):
    account_ref = payload.get("account_ref")
    profile_root = payload.get("profile_root")
    if not account_ref and not profile_root:
        return None
    if not isinstance(account_ref, str) or not isinstance(profile_root, str):
        raise TypeError("Connection profile payload is invalid")
    profile = ProfileNamespace(Path(profile_root), SOURCE_REGISTRY).profile(
        "bilibili", account_ref
    )
    return profile


def _browser_cookie_profile(profile_path: Path) -> Path:
    """Point yt-dlp at the Chromium profile below our user-data directory."""
    default_profile = profile_path / "Default"
    return default_profile if default_profile.is_dir() else profile_path


def verify_video_file(path: Path, ffmpeg_exe: str) -> None:
    """Decode a bounded sample before a downloaded file is published."""
    try:
        if path.suffix.lower() not in {".mp4", ".mkv", ".webm"} or path.stat().st_size <= 0:
            raise RuntimeError("Bộ tải không trả về file video hợp lệ.")
    except OSError as error:
        raise RuntimeError("Không thể kiểm tra file video tải về.") from error

    executable = Path(ffmpeg_exe)
    if not executable.is_file():
        raise RuntimeError("Backend chưa có ffmpeg để kiểm tra file video tải về.")
    try:
        result = subprocess.run(
            [
                str(executable),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Không thể giải mã mẫu file video tải về.") from error
    if result.returncode != 0:
        raise RuntimeError("File tải về không có video stream giải mã được.")


def download(payload: dict, *, connection_profile=None) -> None:
    import imageio_ffmpeg
    import yt_dlp

    destination = Path(payload["directory"])
    guard_public_network()

    class Logger:
        def debug(self, message):
            pass

        def warning(self, message):
            pass

        def error(self, message):
            pass

    def progress(data):
        info = data.get("info_dict") or {}
        emit("progress", phase="merging" if data["status"] == "finished" else "downloading",
             downloaded_bytes=data.get("downloaded_bytes") or 0,
             total_bytes=data.get("total_bytes") or data.get("total_bytes_estimate"),
             speed=data.get("speed"), eta=data.get("eta"),
             title=info.get("title"), duration=info.get("duration"))

    def check_video(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            return "Link này là phát trực tiếp. Hãy dùng link video đã đăng."
        if info.get("vcodec") == "none":
            return "Link này chỉ chứa âm thanh, không có video."
        for item in info.get("requested_formats") or [info]:
            protocol = item.get("protocol")
            if protocol and any(part not in {"http", "https", "m3u8_native", "http_dash_segments"}
                                for part in protocol.split("+")):
                return "Giao thức video chưa được hỗ trợ."
        return None

    height = payload["quality"]
    limit = "" if height == "best" else f"[height<=?{height}]"
    completed_paths = []
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    options = {
        # Separate formats/video IDs so refreshed extraction can't append different content.
        "outtmpl": str(destination / "video.%(id)s.%(format_id)s.%(ext)s"),
        "continuedl": True,
        "nopart": False,
        "overwrites": False,
        "skip_unavailable_fragments": False,
        "post_hooks": [lambda filename: completed_paths.append(Path(filename))],
        "format": f"bv*{limit}+ba/b{limit}",
        "format_sort": ["vcodec:h264", "acodec:aac"],
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "ffmpeg_location": ffmpeg_exe,
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "logger": Logger(),
        "progress_hooks": [progress],
        "postprocessor_hooks": [lambda _: emit("progress", phase="merging")],
        "match_filter": check_video,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 2,
        "hls_prefer_native": True,
        "cachedir": False,
        "proxy": "",  # Direct connections so the address guard covers each request.
        "enable_file_urls": False,
        "js_runtimes": {"node": {}},
    }
    if payload.get("cookie_file"):
        options["cookiefile"] = payload["cookie_file"]
    if connection_profile is not None:
        options["cookiesfrombrowser"] = (
            "chromium",
            str(_browser_cookie_profile(connection_profile.path)),
            None,
            None,
        )
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(payload["url"], download=True)
        if info and info.get("entries") is not None:
            info = next((entry for entry in info["entries"] if entry), None)
        candidates = [path for path in completed_paths
                      if path.suffix in {".mp4", ".mkv", ".webm"} and path.is_file()]
        if not info or len(candidates) != 1:
            raise RuntimeError("Không tìm thấy video hoàn chỉnh. Hãy dùng link của một video cụ thể.")
        verify_video_file(candidates[0], ffmpeg_exe)
        emit("complete", filename=candidates[0].name, title=info.get("title") or "Video",
             duration=info.get("duration"), platform=info.get("extractor_key") or info.get("extractor"))
    except Exception as error:
        emit("error", message=download_error_message(error))
        sys.exit(1)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.readline())
        with work_lock(Path(payload["directory"])):
            profile = _connection_profile(payload)
            if profile is None:
                download(payload)
            else:
                owner_id = f"video-download-{str(payload.get('job_id') or 'worker')[:80]}"
                with ProfileLock(profile, owner_id=owner_id):
                    download(payload, connection_profile=profile)
    except Exception as error:
        emit("error", message=download_error_message(error))
        sys.exit(1)


if __name__ == "__main__":
    main()
