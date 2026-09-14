import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.services.voiceover.audio_cache import AudioCache


def entry(root, key, size=100):
    root.mkdir(parents=True, exist_ok=True)
    (root / f'{key}.wav').write_bytes(b'x' * size)
    (root / f'{key}.json').write_bytes(b'{}')


def test_lru_both_limits_and_pinned_audio_with_multiple_cache_instances(tmp_path):
    keys = ['a' * 64, 'b' * 64, 'c' * 64]
    for key in keys:
        entry(tmp_path, key)
    first = AudioCache(tmp_path, max_bytes=220, max_entries=2)
    for index, key in enumerate(keys):
        with first.metadata() as connection:
            connection.execute('INSERT INTO usage VALUES (?, ?)', (key, index))
    lease = first.pin(keys[0])
    second = AudioCache(tmp_path, max_bytes=220, max_entries=2)
    assert second.trim()['removed'] == [keys[1]]
    assert (tmp_path / f'{keys[0]}.wav').exists()
    lease.release()
    with second.metadata() as connection:
        assert connection.execute('SELECT COUNT(*) FROM leases').fetchone()[0] == 0
    # Newest access survives the count limit even when byte capacity is plentiful.
    small = AudioCache(tmp_path, max_bytes=9999, max_entries=1)
    assert small.trim()['removed'] == [keys[2]]


def test_capacity_reservation_rejects_before_deleting_in_use_files_and_expires_abandoned_pins(tmp_path):
    key = 'd' * 64
    entry(tmp_path, key)
    cache = AudioCache(tmp_path, max_bytes=150, max_entries=2)
    lease = cache.pin(key)
    with pytest.raises(ValueError, match='đang đầy'):
        cache.trim(reserve_bytes=100, new_key='e' * 64)
    assert (tmp_path / f'{key}.wav').exists()
    with cache.metadata() as connection:
        connection.execute('UPDATE leases SET expires = ?', (time.time() - 1,))
    assert cache.trim(reserve_bytes=100, new_key='e' * 64)['removed'] == [key]
    lease.release()


def test_abandoned_encoder_files_are_cleaned_without_touching_unrelated_files(tmp_path):
    cache = AudioCache(tmp_path)
    part = tmp_path / f'{"a" * 64}.{"b" * 32}.part.wav'
    unrelated = tmp_path / 'my-recording.wav'
    part.write_bytes(b'partial')
    unrelated.write_bytes(b'keep')
    with cache.encoder():
        cache.clean_abandoned_parts()
    assert not part.exists() and unrelated.read_bytes() == b'keep'


def test_encoder_excludes_another_process_cancels_wait_and_unlocks_after_process_death(tmp_path):
    lock, ready = tmp_path / '.encoder.sqlite3', tmp_path / 'ready'
    script = '''import sys, time
from pathlib import Path
from app.services.voiceover.audio_cache import database_lock
with database_lock(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text('locked')
    time.sleep(30)
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(lock), str(ready)],
        cwd=Path(__file__).parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), 'Child did not acquire the real SQLite process lock'
        cancel = threading.Event()
        timer = threading.Timer(.2, cancel.set)
        timer.start()
        try:
            with pytest.raises(InterruptedError), AudioCache(tmp_path).encoder(cancel):
                pytest.fail('Second process acquired the held encoder lock')
        finally:
            timer.cancel()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        process.stderr.close()
    with AudioCache(tmp_path).encoder():
        pass
