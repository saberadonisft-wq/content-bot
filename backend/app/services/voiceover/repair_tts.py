"""Generate isolated repair attempts in one bounded, existing VieNeu worker batch."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

from .audio import audio_metadata
from .manager import RUNTIME, keep_worker_alive
from .models import VoiceClip
from .store import digest, generation_hash, normalized_text, read_json, write_json
from .sync_worker import _stop_tree


def generate_repair_batch(manager, owner, voice, budget, candidates: list[dict], *, device: str,
                          cancel, check_current, worker_script=None) -> list[dict]:
    """Never attach candidates or overwrite the normal generation-hash cache.

    Same-text retries use a distinct output key, so the worker actually retries them.
    Budget is committed before process launch; all alternatives count separately.
    """
    budget.check(cancel)
    check_current()
    if not candidates or len(candidates) > 40:
        raise ValueError('Batch sửa phải có từ 1 đến 40 ứng viên.')
    if device not in {'cpu', 'cuda'}:
        raise ValueError('Thiết bị tạo giọng không hợp lệ.')
    clips = {clip.id: clip for clip in voice.clips}
    staged = []
    for candidate in candidates:
        original = clips[candidate['clip_id']]
        for member_id in candidate.get('member_ids', [original.id]):
            member = clips[member_id]
            if member.sync.text_locked or member.sync.timing_locked or member.sync.timing_origin != 'automatic':
                raise ValueError('Không tự sinh lại lời hoặc mốc đã khóa.')
        proposed = VoiceClip.model_validate({**original.model_dump(), 'spoken_text': candidate['spoken_text']})
        staged.append(proposed)
    manifest = manager.manifest(owner, voice, device, [])
    root = manager.store.owner_root(owner) / 'repair-work' / uuid.uuid4().hex
    root.mkdir(parents=True)
    reference = voice.profile.reference_id
    if reference:
        shutil.copyfile(manager.store.path(owner, 'references', reference, '.wav'), root / 'reference.wav')
    # A failed launch consumes its reservation too; it cannot replay after a crash.
    receipt = budget.reserve('tts', [member for candidate in candidates
        for member in candidate.get('member_ids', [candidate['clip_id']])], cancel=cancel)
    process = heartbeat = None
    settled = False
    try:
        entries = []
        for index, (candidate, clip) in enumerate(zip(candidates, staged, strict=True)):
            key = digest({'repair_receipt': receipt, 'index': index, 'input': candidate})
            entries.append({'id': key, 'generation_hash': key,
                            'text': normalized_text(clip.spoken_text, voice.pronunciation)})
        manifest['clips'] = entries
        write_json(root / 'manifest.json', manifest)
        write_json(root / 'attempts.json', {'receipt_id': receipt, 'candidates': candidates,
                                           'input_binding': budget.snapshot()['input_binding']})
        heartbeat_path = root / 'supervisor.heartbeat'
        heartbeat = keep_worker_alive(heartbeat_path)
        threads = '2' if device == 'cuda' else '3'
        env = dict(os.environ, PYTHONUTF8='1', HF_HUB_DISABLE_TELEMETRY='1', VOICE_ALLOW_DOWNLOAD='0',
                   CONTENT_BOT_VOICE_THREADS=threads, CONTENT_BOT_VOICE_BATCH_SIZE='4',
                   VOICE_SUPERVISOR_HEARTBEAT=str(heartbeat_path.resolve()),
                   VOICE_WORKER_LOCK=str((RUNTIME / '.worker.lock').resolve()))
        budget.check(cancel)
        check_current()
        with (root / 'worker.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(manager.python(device)), str(worker_script or RUNTIME / 'worker.py'),
                'run', str(root)], stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                if os.name == 'nt' else 0, start_new_session=os.name != 'nt')
            while process.poll() is None:
                budget.check(cancel)
                check_current()
                cancel.wait(.1)
        budget.check(cancel)
        check_current()
        outputs = []
        for candidate, clip, item in zip(candidates, staged, entries, strict=True):
            key = item['generation_hash']
            wav = root / 'assets' / f'{key}.wav'
            sidecar = root / 'assets' / f'{key}.json'
            result = {**candidate, 'attempt_id': key, 'receipt_id': receipt, 'state': 'failed'}
            if wav.is_file() and sidecar.is_file():
                meta = audio_metadata(wav)
                committed = read_json(sidecar)
                if committed.get('checksum') != meta['checksum'] or committed.get('generation_hash') != key:
                    result['error'] = 'Checkpoint WAV sửa không khớp.'
                else:
                    meta.update(id=key, generation_hash=generation_hash(voice, clip, device), device=device)
                    # Ordinary assets are immutable; this attempt is unreferenced until checked/applied.
                    target = manager.store.path(owner, 'assets', key, '.wav')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    partial = target.with_suffix('.part')
                    try:
                        shutil.copyfile(wav, partial)
                        partial.replace(target)
                    finally:
                        partial.unlink(missing_ok=True)
                    write_json(manager.store.path(owner, 'assets', key), meta)
                    result.update(state='generated', asset=meta)
            else:
                result['error'] = 'Worker chưa tạo được WAV hoàn chỉnh; giữ bản trước.'
            outputs.append(result)
            write_json(root / 'results.json', outputs)
        budget.settle(receipt, succeeded=all(row['state'] == 'generated' for row in outputs),
                      error=None if process.returncode == 0 else f'Worker exit {process.returncode}')
        settled = True
        write_json(root / 'results.json', outputs)
        return outputs
    finally:
        if heartbeat:
            heartbeat.set()
        if process is not None and process.poll() is None:
            _stop_tree(process.pid)
            process.wait(timeout=10)
        if not settled:
            budget.settle(receipt, succeeded=False, error='Lượt TTS bị dừng hoặc không hoàn tất.')
