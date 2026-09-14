import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_voice_sync_audit import setup

from app.services.subtitle_jobs import SubtitleJobCanceled
from app.services.voiceover import sync_worker as module
from app.services.voiceover.store import read_json
from app.services.voiceover.sync_audit import audit_path


def alive(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_actual_worker_preserves_project_and_returns_audit(tmp_path):
    store, doc, subtitles, media = setup(tmp_path)
    media['has_audio'] = False
    before = store.path('user', 'projects', doc.project_id).read_bytes()
    result = module.run_sync_audit_worker(store, 'user', 'c' * 32, Path('unused'), media,
        doc, subtitles, ['one'], align_source=False, model_dir=tmp_path, timeout_seconds=10)
    assert result['rows'] == 1
    record = read_json(audit_path(store, 'user', result['sync_audit_id']))
    assert record['state'] == 'succeeded'
    assert record['rows'][0]['audio']['duration_ms'] == 500
    assert 'source_unverified' in record['rows'][0]['issues']
    assert store.path('user', 'projects', doc.project_id).read_bytes() == before
    assert not list(audit_path(store, 'user', 'c' * 32).parent.glob('c' * 32 + '-*'))


@pytest.mark.parametrize('mode', ['cancel', 'timeout', 'edited', 'resaved'])
def test_hard_stop_reaps_blocked_worker_and_descendant(tmp_path, monkeypatch, mode):
    store, doc, subtitles, media = setup(tmp_path)
    marker = tmp_path / 'descendant.pid'
    script = tmp_path / 'blocked.py'
    script.write_text('''import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(120)
''', encoding='utf-8')
    original = subprocess.Popen
    processes = []
    cancel = threading.Event()

    def launch(command, **kwargs):
        if 'app.services.voiceover.sync_worker' in command:
            assert kwargs['env']['CUDA_VISIBLE_DEVICES'] == ''
            assert kwargs['env']['OMP_NUM_THREADS'] == '3'
            assert kwargs['env']['HF_HUB_OFFLINE'] == '1'
            command = [sys.executable, str(script), str(marker)]
            process = original(command, **kwargs)
            processes.append(process)
            return process
        return original(command, **kwargs)

    monkeypatch.setattr(module.subprocess, 'Popen', launch)

    def request_cancel():
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        if mode in {'edited', 'resaved'}:
            current = store.get_document('user', doc.project_id)
            if mode == 'edited':
                current.clips[0].offset_ms += 50
            store.save_document('user', current)
        else:
            cancel.set()

    watcher = threading.Thread(target=request_cancel) if mode != 'timeout' else None
    if watcher:
        watcher.start()
    expected = SubtitleJobCanceled if mode in {'cancel', 'edited'} else TimeoutError
    started = time.monotonic()
    try:
        with pytest.raises(expected):
            module.run_sync_audit_worker(store, 'user', 'd' * 32, Path('unused'), media,
                doc, subtitles, ['one'], align_source=True, model_dir=tmp_path, cancel=cancel,
                timeout_seconds=8 if mode in {'cancel', 'edited'} else 2)
        assert processes and processes[0].poll() is not None
        assert marker.exists(), 'Fixture never started its descendant'
        assert not alive(int(marker.read_text())), 'Inference descendant survived hard stop'
        record = read_json(audit_path(store, 'user', 'd' * 32))
        assert record['state'] == ('canceled' if mode in {'cancel', 'edited'} else 'failed')
        if mode == 'edited':
            assert store.get_document('user', doc.project_id).clips[0].offset_ms == doc.clips[0].offset_ms + 50
        assert time.monotonic() - started < 7
    finally:
        if watcher:
            watcher.join(timeout=3)
        for process in processes:
            if process.poll() is None:
                module._stop_tree(process.pid)
                process.wait(timeout=10)


def test_worker_watcher_stops_inference_when_api_parent_exits(tmp_path):
    marker, stop = tmp_path / 'family.json', tmp_path / 'parent-exit'
    child_script = tmp_path / 'child.py'
    child_script.write_text('''import json, os, subprocess, sys, threading, time
from pathlib import Path
from app.services.voiceover.sync_worker import _watch_parent
threading.Thread(target=_watch_parent, args=(int(sys.argv[1]),), daemon=True).start()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
Path(sys.argv[2]).write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(120)
''', encoding='utf-8')
    parent_script = tmp_path / 'parent.py'
    parent_script.write_text('''import os, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, sys.argv[1], str(os.getpid()), sys.argv[2]],
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
while not Path(sys.argv[3]).exists(): time.sleep(.05)
''', encoding='utf-8')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    parent = subprocess.Popen([sys.executable, str(parent_script), str(child_script), str(marker), str(stop)],
        env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
    family = []
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert marker.exists()
        family = json.loads(marker.read_text())
        assert all(alive(pid) for pid in family)
        stop.touch()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in family) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not any(alive(pid) for pid in family)
    finally:
        if parent.poll() is None:
            module._stop_tree(parent.pid)
            parent.wait(timeout=10)
        if family and alive(family[0]):
            module._stop_tree(family[0])
