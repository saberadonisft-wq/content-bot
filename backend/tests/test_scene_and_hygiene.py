import json
from pathlib import Path
from types import SimpleNamespace

from app.services.scene_splitter import (
    export_scene_chunks,
    group_scenes_into_chunks,
    split_subtitles_for_chunk,
)
from app.services.system_hygiene import (
    BROWSER_PROCESS_OWNERSHIP,
    BrowserProcessOwnership,
    BrowserProcessOwnershipRegistry,
    cleanup_orphaned_browser_processes,
    lower_current_process_priority,
    verify_onnx_runtime_health,
)


def test_group_scenes_into_chunks():
    # Cuts at 0s, 15s, 35s, 50s, 80s, 110s, 125s
    cuts = [0.0, 15.0, 35.0, 50.0, 80.0, 110.0, 125.0]

    # Target 45s, min 25s, max 75s
    chunks = group_scenes_into_chunks(cuts, target_duration_s=45.0, min_duration_s=25.0, max_duration_s=75.0)

    assert len(chunks) >= 2
    # First chunk should cut at 50.0s (0 to 50s = 50s, >= 45s)
    assert chunks[0] == (0.0, 50.0)
    # Second chunk: 50.0s to 110.0s (60s)
    assert chunks[1][0] == 50.0
    # Final chunk should end at 125.0
    assert chunks[-1][1] == 125.0


def test_detect_scene_cuts_keeps_real_video_bounds(tmp_path):
    import subprocess

    import imageio_ffmpeg

    from app.services.scene_splitter import detect_scene_cuts

    video = tmp_path / "cuts.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=15:d=2",
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=15:d=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True, capture_output=True)
    cuts = detect_scene_cuts(video, threshold=5, min_scene_len_s=0.5, duration_s=4)
    assert cuts[0] == 0
    assert cuts[-1] == 4
    assert any(1.5 < cut < 2.5 for cut in cuts[1:-1])


def test_split_subtitles_for_chunk():
    doc = {
        "segments": [
            {"id": "s1", "start_ms": 2000, "end_ms": 5000, "text": "Đoạn 1"},
            {"id": "s2", "start_ms": 10000, "end_ms": 15000, "text": "Đoạn 2"},
            {"id": "s3", "start_ms": 40000, "end_ms": 45000, "text": "Đoạn 3"},
        ]
    }

    # Slice chunk from 8s to 30s
    chunk_doc = split_subtitles_for_chunk(doc, 8.0, 30.0)
    segs = chunk_doc["segments"]

    # Only s2 falls in [8s, 30s]
    assert len(segs) == 1
    assert segs[0]["text"] == "Đoạn 2"
    # Re-indexed relative to 8000ms: 10000 - 8000 = 2000ms
    assert segs[0]["start_ms"] == 2000
    assert segs[0]["end_ms"] == 7000


def test_export_scene_chunks_writes_video_subtitle_and_manifest(tmp_path):
    import subprocess

    import imageio_ffmpeg

    video = tmp_path / "source.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True, capture_output=True)
    result = export_scene_chunks(
        video,
        tmp_path / "shorts",
        [(0, 1), (1, 2)],
        source_fingerprint="f" * 64,
        subtitles_doc={"segments": [{"id": "cue", "start_ms": 200, "end_ms": 800, "text": "Xin chào"}]},
    )
    assert len(result) == 2
    assert all(Path(item["video_path"]).is_file() for item in result)
    assert Path(result[0]["srt_path"]).read_text(encoding="utf-8").startswith("1\n")
    manifest = Path(result[0]["manifest_path"])
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["source_fingerprint"] == "f" * 64
    assert [item["duration_s"] for item in payload["chunks"]] == [1.0, 1.0]


def test_verify_onnx_runtime_health():
    info = verify_onnx_runtime_health()
    assert "installed" in info
    assert "has_cuda" in info
    assert "providers" in info


def test_lower_current_process_priority():
    res = lower_current_process_priority()
    # On Windows, this should succeed or safely return bool
    assert isinstance(res, bool)


def test_cleanup_orphaned_browser_processes_safe():
    count = cleanup_orphaned_browser_processes(profile_pattern="non_existent_mock_profile_xyz")
    assert count == 0


def test_cleanup_browser_requires_matching_owned_identity(monkeypatch):
    from app.services import system_hygiene

    monkeypatch.setattr(system_hygiene.platform, "system", lambda: "Windows")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "powershell":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "ProcessId": 123,
                    "ParentProcessId": 7,
                    "SessionId": 4,
                    "CreationDate": "20260915120000.000000+000",
                    "CommandLine": "chrome --profile content_bot_browser_profile",
                }),
            )
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(system_hygiene.subprocess, "run", run)
    owned = BrowserProcessOwnership(
        pid=123,
        parent_pid=7,
        session_id=4,
        creation_time="20260915120000.000000+000",
    )
    assert cleanup_orphaned_browser_processes(ownership=[owned]) == 0
    assert len(calls) == 1
    assert cleanup_orphaned_browser_processes(ownership=[owned], terminate=True) == 1
    assert calls[-1][0] == "taskkill"


def test_cleanup_browser_rejects_session_identity_mismatch(monkeypatch):
    from app.services import system_hygiene

    monkeypatch.setattr(system_hygiene.platform, "system", lambda: "Windows")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "ProcessId": 123,
                "ParentProcessId": 7,
                "SessionId": 5,
                "CreationDate": "created",
                "CommandLine": "chrome --profile content_bot_browser_profile",
            }) if command[0] == "powershell" else "",
        )

    monkeypatch.setattr(system_hygiene.subprocess, "run", run)
    owned = BrowserProcessOwnership(pid=123, creation_time="created", parent_pid=7, session_id=4)
    assert cleanup_orphaned_browser_processes(ownership=[owned], terminate=True) == 0
    assert len(calls) == 1


def test_browser_process_ownership_registry_tracks_exact_identity():
    registry = BrowserProcessOwnershipRegistry()
    owned = BrowserProcessOwnership(pid=44, creation_time="created", parent_pid=2)
    registry.register(owned)
    assert registry.snapshot() == (owned,)
    registry.unregister(BrowserProcessOwnership(pid=44, creation_time="other"))
    assert registry.snapshot() == (owned,)
    registry.unregister(owned)
    assert registry.snapshot() == ()


def test_cleanup_uses_shared_registry_when_ownership_is_omitted(monkeypatch):
    from app.services import system_hygiene

    monkeypatch.setattr(system_hygiene.platform, "system", lambda: "Windows")
    BROWSER_PROCESS_OWNERSHIP.clear()
    owned = BrowserProcessOwnership(pid=321, creation_time="created")
    BROWSER_PROCESS_OWNERSHIP.register(owned)
    monkeypatch.setattr(
        system_hygiene.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "ProcessId": 321,
                "ParentProcessId": 1,
                "CreationDate": "created",
                "CommandLine": "chrome --profile content_bot_browser_profile",
            }),
        ),
    )
    try:
        assert cleanup_orphaned_browser_processes() == 0
    finally:
        BROWSER_PROCESS_OWNERSHIP.clear()
