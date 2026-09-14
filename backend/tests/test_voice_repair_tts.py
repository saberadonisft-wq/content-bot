import sys
import threading
import time

import pytest
from test_voice_sync_audit import setup
from test_voice_sync_worker import alive

from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.repair_budget import RepairBudget, RepairBudgetExceeded
from app.services.voiceover.repair_tts import generate_repair_batch
from app.services.voiceover.store import generation_hash, read_json


def context(tmp_path, monkeypatch):
    store, doc, _, _ = setup(tmp_path)
    manager = VoiceManager(store)
    monkeypatch.setattr(manager, 'python', lambda device: sys.executable)
    budget = RepairBudget(tmp_path / 'budget.json', input_binding='a' * 64, cluster_ids=['one'])
    return store, doc, manager, budget


def test_one_worker_batch_generates_two_distinct_same_text_attempts_without_attaching(tmp_path, monkeypatch):
    store, doc, manager, budget = context(tmp_path, monkeypatch)
    script = tmp_path / 'fixture.py'
    script.write_text('''import hashlib, json, os, struct, sys, wave
from pathlib import Path
root = Path(sys.argv[2])
assert os.environ['CONTENT_BOT_VOICE_THREADS'] == '3'
assert os.environ['VOICE_ALLOW_DOWNLOAD'] == '0'
assert json.loads((root.parents[3] / 'budget.json').read_text())['tts_used'] == 2
(root / 'assets').mkdir()
for item in json.loads((root / 'manifest.json').read_text())['clips']:
    key = item['generation_hash']
    path = root / 'assets' / (key + '.wav')
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack('<h', 1000) * 2400)
    (root / 'assets' / (key + '.json')).write_text(json.dumps({
        'generation_hash': key, 'checksum': hashlib.sha256(path.read_bytes()).hexdigest()}))
''', encoding='utf-8')
    project = store.path('user', 'projects', doc.project_id)
    before = project.read_bytes()
    original = store.path('user', 'assets', doc.clips[0].asset_id, '.wav')
    wav_before = original.read_bytes()
    item = {'clip_id': 'one', 'spoken_text': doc.clips[0].spoken_text, 'mode': 'retry_original'}
    try:
        results = generate_repair_batch(manager, 'user', doc, budget, [item, item], device='cpu',
            cancel=threading.Event(), check_current=lambda: None, worker_script=script)
        assert all(row['state'] == 'generated' for row in results)
        assert len({row['asset']['id'] for row in results}) == 2
        assert all(row['asset']['generation_hash'] == generation_hash(doc, doc.clips[0], 'cpu') for row in results)
        assert all(row['asset']['id'] != doc.clips[0].asset_id for row in results)
        assert project.read_bytes() == before and original.read_bytes() == wav_before
        assert budget.snapshot()['tts_used'] == 2
        assert len(list(store.owner_root('user').glob('repair-work/*/manifest.json'))) == 1
    finally:
        manager.shutdown()


@pytest.mark.parametrize('mode', ['cancel', 'deadline', 'edit'])
def test_repair_tts_hard_stops_worker_and_child_and_keeps_old_project(tmp_path, monkeypatch, mode):
    store, doc, manager, budget = context(tmp_path, monkeypatch)
    marker = tmp_path / 'child.pid'
    script = tmp_path / 'blocked.py'
    script.write_text('''import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
Path(sys.argv[0]).with_name('child.pid').write_text(str(child.pid))
time.sleep(120)
''', encoding='utf-8')
    project = store.path('user', 'projects', doc.project_id)
    before = project.read_bytes()
    cancel = threading.Event()

    def check_current():
        if marker.exists():
            if mode == 'edit':
                raise ValueError('fixture obsolete inputs')
            if mode == 'deadline':
                budget.deadline = time.time() - 1
            else:
                cancel.set()

    try:
        expected = ValueError if mode == 'edit' else RepairBudgetExceeded if mode == 'deadline' else InterruptedError
        with pytest.raises(expected):
            generate_repair_batch(manager, 'user', doc, budget,
                [{'clip_id': 'one', 'spoken_text': doc.clips[0].spoken_text}], device='cpu',
                cancel=cancel, check_current=check_current, worker_script=script)
        assert marker.exists() and not alive(int(marker.read_text()))
        assert project.read_bytes() == before and budget.snapshot()['tts_used'] == 1
        assert next(iter(read_json(budget.path)['receipts'].values()))['state'] == 'failed'
    finally:
        manager.shutdown()
