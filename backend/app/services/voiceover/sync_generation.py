"""Continue a user-started voice job through bounded source checks, repair and apply."""
from __future__ import annotations

import threading
import time
import uuid

from ..media_probe import probe_media_cached
from .repair_pipeline import run_sync_repair
from .store import read_json, write_json
from .sync_apply import apply_sync_candidates
from .sync_audit import audit_path, snapshot_binding
from .sync_worker import run_sync_audit_worker


def finish_generation_sync(manager, service, owner, job, persist, *, settings,
                            audit=run_sync_audit_worker, repair=run_sync_repair, apply=apply_sync_candidates):
    store = manager.store
    root = store.owner_root(owner) / 'sync-generation' / job['sync_session_id']
    inputs = read_json(root / 'input.json')
    subtitles = inputs['document']
    voice = store.get_document(owner, job['project_id'])
    binding = snapshot_binding(voice, subtitles)
    state_path = root / 'checkpoint.json'
    state = read_json(state_path) if state_path.exists() else {
        'input_binding': binding, 'audit_id': uuid.uuid4().hex, 'rows': [], 'warnings': [],
        'checked_ids': [], 'source_deadline': time.time() + 600, 'stage': 'source'}
    if state['input_binding'] != binding:
        if state.get('applied_binding') == binding:
            job.update(state='succeeded', phase='finished', message='Đã tạo và căn các đoạn đủ bằng chứng',
                       sync_result=state.get('summary'))
        else:
            job.update(state='succeeded', phase='finished',
                message='Đã tạo giọng; đầu vào đã đổi nên giữ bản chỉnh và bỏ lượt căn cũ.',
                sync_result={'state': 'obsolete'})
        persist()
        return
    write_json(state_path, state)
    write_json(audit_path(store, owner, state['audit_id']).with_suffix('.job.json'),
               {'job_id': None, 'voice_job_id': job['id'], 'project_id': voice.project_id})
    stop = threading.Event()
    finished = threading.Event()

    def watch_control():
        while not finished.wait(.1):
            if manager.controls.get(job['id']) or not manager._accepting:
                stop.set()
                return

    watcher = threading.Thread(target=watch_control, daemon=True, name='voice-sync-control')
    watcher.start()

    def progress(percent, phase, message):
        job.update(phase='sync', sync_phase=phase, sync_progress=percent, message=message, eta_seconds=None)
        persist()

    try:
        # Resolve only the uploaded project asset; no caller-controlled filesystem path.
        from ...api.media_paths import _uploaded_video_path
        video = _uploaded_video_path(job['project_id'])
        media = probe_media_cached(video, settings.data_dir / 'cache' / 'media-probes')
        if media['fingerprint'] != voice.video_fingerprint:
            raise ValueError('Video không còn khớp dự án giọng.')
        requested_ids = set(inputs['clip_ids'])
        candidates = [clip for clip in voice.clips if clip.id in requested_ids
                      and clip.sync.timing_origin == 'automatic' and not clip.sync.timing_locked]
        # Prioritize unresolved duration/overlap issues; still count only actually audited clusters.
        candidates.sort(key=lambda clip: (not bool(clip.sync.issues), clip.start_ms, clip.id))
        pending = [clip.id for clip in candidates if clip.id not in state['checked_ids']]
        if state['stage'] == 'source':
            for offset in range(0, len(pending), 40):
                if stop.is_set():
                    raise InterruptedError('Đã dừng kiểm tra nguồn.')
                remaining = state['source_deadline'] - time.time()
                if remaining <= 0:
                    state['warnings'].append({'code': 'source_phase_budget', 'message': 'Hết 10 phút kiểm tra nguồn; giữ các vùng chưa kiểm tra.'})
                    break
                selected = pending[offset:offset + 40]
                identifier = uuid.uuid4().hex
                try:
                    audit(store, owner, identifier, video, media, voice, subtitles, selected, align_source=True,
                        model_dir=settings.data_dir / 'models' / 'faster-whisper',
                        whisper_model=settings.content_bot_alignment_whisper_model,
                        cancel=stop, progress=progress, timeout_seconds=min(600, remaining))
                    result = read_json(audit_path(store, owner, identifier))
                    state['rows'].extend(result['rows'])
                    state['warnings'].extend(result.get('warnings', []))
                    state['checked_ids'].extend(selected)
                    write_json(state_path, state)
                except TimeoutError:
                    state['warnings'].append({'code': 'source_phase_budget', 'message': 'Lượt kiểm tra nguồn đã chạm giới hạn thời gian.'})
                    break
            state['stage'] = 'repair'
            write_json(state_path, state)
        if stop.is_set():
            raise InterruptedError('Đã dừng trước bước sửa giọng.')
        if snapshot_binding(store.get_document(owner, voice.project_id), subtitles) != binding:
            raise ValueError('Dự án đã thay đổi trong lúc kiểm tra nguồn.')
        aggregate = {'id': state['audit_id'], 'project_id': voice.project_id, 'voice_revision': voice.revision,
            'source_fingerprint': media['fingerprint'], 'input_binding': binding, 'source_document': subtitles,
            'state': 'succeeded', 'clip_ids': state['checked_ids'], 'rows': state['rows'],
            'warnings': state['warnings'], 'automatic_apply': False}
        write_json(audit_path(store, owner, state['audit_id']), aggregate)
        if state['checked_ids']:
            repaired = repair(manager, service, owner, state['audit_id'], video, media, subtitles,
                device=job['device'], model_dir=settings.data_dir / 'models' / 'faster-whisper',
                cancel=stop, progress=progress, evaluated_clip_ids=state['checked_ids'])
            result_id = repaired['sync_audit_id']
            result = read_json(audit_path(store, owner, result_id))
        else:
            result_id, result = state['audit_id'], aggregate
        eligible = [row['clip_id'] for row in result['rows'] if row.get('completed')
                    and row.get('processed', {}).get('state') == 'ready_for_review']
        applied = 0
        if eligible:
            if stop.is_set():
                raise InterruptedError('Đã dừng trước khi áp kết quả.')
            progress(98, 'sync_apply', 'Đang lưu các đoạn đã đạt kiểm tra')
            current = store.get_document(owner, voice.project_id)
            current = apply(store, owner, result_id, project_id=voice.project_id, revision=current.revision,
                subtitles=subtitles, clip_ids=eligible, media=media)
            applied = len(eligible)
            state['applied_binding'] = snapshot_binding(current, subtitles)
        state['stage'] = 'finished'
        verified_ids = {clip.id for clip in store.get_document(owner, voice.project_id).clips
                        if clip.sync.state == 'aligned'}
        state['summary'] = {'state': 'succeeded', 'audit_id': result_id, 'checked': len(state['checked_ids']),
                            'applied': applied, 'unverified': sum(clip.id not in verified_ids for clip in candidates),
                            'warnings': state['warnings'], 'repair_budget': result.get('repair_budget')}
        write_json(state_path, state)
        # Register for the existing panel/listen/apply routes without a second job queue.
        write_json(audit_path(store, owner, result_id).with_suffix('.job.json'),
                   {'job_id': None, 'voice_job_id': job['id'], 'project_id': voice.project_id})
        final_voice = store.get_document(owner, voice.project_id)
        ready_ids = {clip.id for clip in final_voice.clips if clip.id in requested_ids and clip.asset_id
                     and clip.status not in {'stale', 'failed', 'missing'}}
        if 'failed' in job:
            job['failed'] = [failure for failure in job['failed'] if failure['clip_id'] not in ready_ids]
            failures = {failure['clip_id'] for failure in job['failed']}
            job['failed'].extend({'clip_id': clip.id, 'error': 'Chưa có WAV đạt sau lượt sửa có giới hạn.'}
                for clip in final_voice.clips if clip.id in requested_ids - ready_ids - failures)
        job.update(completed=len(ready_ids), completed_clip_ids=sorted(ready_ids))
        job.update(state='failed' if job.get('failed') else 'succeeded', phase='finished', sync_result=state['summary'],
            message=f'Đã xử lý giọng; tự căn {applied} đoạn, còn {len(requested_ids - ready_ids)} đoạn chưa có WAV đạt. '
                    'Giữ các vùng chưa đủ bằng chứng để kiểm tra.')
    except Exception as exc:
        action = manager.controls.get(job['id'])
        incomplete = bool(job.get('failed')) or job.get('completed', 0) < job.get('total', 0)
        job.update(state='paused' if action == 'pause' else 'canceled' if stop.is_set() else 'failed' if incomplete else 'succeeded',
            phase='sync_stopped', sync_result={'state': 'canceled' if stop.is_set() else 'needs_review',
                'message': str(exc)[:500], 'audit_id': state['audit_id']},
            message='Đã giữ giọng đã tạo. ' + str(exc)[:500])
        write_json(state_path, state)
    finally:
        finished.set()
        watcher.join(timeout=2)
        persist()
