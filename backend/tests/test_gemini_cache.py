import os
import time

from app.services.gemini_cache import cache_session


def test_cache_retention_never_prunes_an_active_session(tmp_path):
    with cache_session(tmp_path, retention_days=7, max_bytes=10):
        directory = tmp_path / "checkpoints" / ("a" * 64)
        directory.mkdir(parents=True)
        checkpoint = directory / "chunk.json"
        checkpoint.write_bytes(b"x" * 20)
        with cache_session(tmp_path, retention_days=7, max_bytes=10):
            assert checkpoint.exists()
    with cache_session(tmp_path, retention_days=7, max_bytes=10):
        assert not checkpoint.exists()


def test_cache_retention_respects_age_and_ignores_unowned_paths(tmp_path):
    old = tmp_path / "media-cache" / ("b" * 64)
    old.mkdir(parents=True)
    checkpoint = old / "manifest.json"
    checkpoint.write_text("{}")
    timestamp = time.time() - 8 * 86400
    os.utime(checkpoint, (timestamp, timestamp))
    os.utime(old, (timestamp, timestamp))
    unowned = tmp_path / "checkpoints" / "user-file"
    unowned.mkdir(parents=True)
    (unowned / "keep.txt").write_text("keep")
    with cache_session(tmp_path, retention_days=7, max_bytes=1000):
        assert not old.exists()
        assert (unowned / "keep.txt").exists()
