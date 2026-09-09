"""Real local-worker smoke test in isolated artifacts; never touches production projects."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.models import VoiceClip, VoiceDocument, VoiceProfile
from app.services.voiceover.store import VoiceStore

root = Path(__file__).resolve().parents[2] / "artifacts/voiceover/integration"
store = VoiceStore(root)
manager = VoiceManager(store)
project = "b" * 20
try:
    try:
        document = store.get_document("smoke", project)
    except FileNotFoundError:
        document = store.save_document("smoke", VoiceDocument(
            project_id=project, video_fingerprint="smoke",
            profile=VoiceProfile(id="ngoc-huyen", name="Ngọc Huyền", preset="Ngọc Huyền"),
            clips=[VoiceClip(id="clip", spoken_text="Cánh cửa vừa mở ra, cô gái đã nhận ra một bí mật bất ngờ.",
                             start_ms=0, end_ms=30000)]))
    job = manager.start("smoke", project, "cpu")
    deadline = time.monotonic() + 180
    while job["state"] in {"queued", "running"}:
        if time.monotonic() > deadline:
            manager.control("smoke", job["id"], "cancel")
            raise TimeoutError("Real worker smoke timed out")
        time.sleep(1)
        job = manager.get("smoke", job["id"])
    print(job, flush=True)
    assert job["state"] == "succeeded", job["message"]
    document = store.get_document("smoke", project)
    assert document.clips[0].asset_id
    print("PASS: real local worker produced and attached audio", flush=True)
finally:
    manager.shutdown()
