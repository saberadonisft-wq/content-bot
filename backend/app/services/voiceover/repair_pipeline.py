"""Checkpointed phase-five repair, after source audit and cheap waveform fitting."""
from __future__ import annotations

import threading
import uuid

from .audio_cache import database_lock
from .repair_budget import RepairBudget, RepairBudgetExceeded
from .repair_queue import select_repairs
from .repair_review import propose_reviewed_wording
from .repair_tts import generate_repair_batch
from .source_context import inspect_source_context
from .source_groups import group_candidates
from .store import read_json, write_json
from .sync_audit import audit_path, snapshot_binding
from .sync_worker import run_sync_audit_worker


def run_sync_repair(manager, service, owner, audit_id, video, media, subtitles, *, device,
                    model_dir, cancel=None, progress=None, evaluated_clip_ids=None,
                    propose=propose_reviewed_wording, generate=generate_repair_batch,
                    inspect=run_sync_audit_worker, context=inspect_source_context):
    """Prepare reviewed WAVs without applying them. Explicit job start is the only caller.

    The output is a normal bound sync audit, so existing listen/apply/undo use it.
    A resumed phase keeps spent attempts, including unfinished reservations.
    """
    cancel = cancel or threading.Event()
    store = manager.store
    original_audit = read_json(audit_path(store, owner, audit_id))
    voice = store.get_document(owner, original_audit['project_id'])
    binding = snapshot_binding(voice, subtitles)
    if (original_audit['state'] != 'succeeded' or original_audit['input_binding'] != binding
            or media['fingerprint'] != voice.video_fingerprint):
        raise ValueError('Lượt kiểm tra không còn khớp dự án; không sửa trên dữ liệu cũ.')
    evaluated = evaluated_clip_ids or original_audit['clip_ids']
    if (not set(evaluated) <= {clip.id for clip in voice.clips}
            or not {row['clip_id'] for row in original_audit['rows']} <= set(evaluated)):
        raise ValueError('Danh sách cụm đánh giá không thuộc dự án.')
    root = store.owner_root(owner) / 'repair-runs' / audit_id
    root.mkdir(parents=True, exist_ok=True)
    with database_lock(root / 'run.lock.sqlite3', cancel):
        path = root / 'checkpoint.json'
        if path.exists():
            record = read_json(path)
            if record['input_binding'] != binding or record['evaluated_clip_ids'] != evaluated:
                raise ValueError('Không dùng lượt sửa cũ cho đầu vào mới.')
            if record['state'] == 'succeeded' and audit_path(store, owner, record['result_audit_id']).is_file():
                return {'sync_audit_id': record['result_audit_id'], 'repair': record}
        else:
            queue, skipped = select_repairs(voice, subtitles, original_audit, media)
            record = {'input_binding': binding, 'evaluated_clip_ids': evaluated,
                      'result_audit_id': uuid.uuid4().hex, 'state': 'running',
                      'queue': queue, 'skipped': skipped, 'wording': {}, 'generated': [],
                      'checked': {}, 'solutions': {}, 'errors': []}
            write_json(path, record)
        budget = RepairBudget(root / 'budget.json', input_binding=binding, cluster_ids=evaluated)
        source_rows = {row['clip_id']: row for row in original_audit['rows']}
        clips = {clip.id: clip for clip in voice.clips}
        project = store.path(owner, 'projects', voice.project_id)
        stat = project.stat()
        observed = (stat.st_mtime_ns, stat.st_size)
        stopped = threading.Event()
        stale = []

        def check_current():
            nonlocal observed
            stat = project.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != observed:
                current = store.get_document(owner, voice.project_id)
                if snapshot_binding(current, subtitles) != binding:
                    raise ValueError('Đầu vào đã thay đổi; giữ bản chỉnh mới và dừng lượt sửa cũ.')
                observed = stamp

        def watch_inputs():
            while not stopped.wait(.2):
                try:
                    check_current()
                except (OSError, ValueError) as exc:
                    stale.append(str(exc)[:500])
                    cancel.set()
                    return

        watcher = threading.Thread(target=watch_inputs, name='repair-input-watch', daemon=True)
        watcher.start()

        def checkpoint():
            record['budget'] = budget.snapshot()
            write_json(path, record)

        def evaluate_pending():
            for generated in record['generated']:
                key, clip_id = generated['attempt_id'], generated['clip_id']
                if generated['state'] != 'generated' or key in record['checked'] or clip_id in record['solutions']:
                    continue
                budget.check(cancel)
                check_current()
                identifier = uuid.uuid4().hex
                original_meta = read_json(store.path(owner, 'assets', clips[clip_id].asset_id)) if clips[clip_id].asset_id else {}
                attempt = {**generated, 'original_asset_id': clips[clip_id].asset_id,
                           'original_checksum': original_meta.get('checksum')}
                try:
                    members = generated.get('member_ids', [clip_id])
                    repair_input = {'kind': 'group', 'source_rows': [source_rows[member] for member in members],
                                    'attempt': attempt} if generated.get('member_ids') else {'row': source_rows[clip_id], 'attempt': attempt}
                    result = inspect(store, owner, identifier, video, media, voice, subtitles, members,
                        align_source=False, model_dir=model_dir, cancel=cancel, progress=progress,
                        timeout_seconds=min(600, budget.remaining_seconds()),
                        repair=repair_input)
                    outputs = read_json(audit_path(store, owner, result['sync_audit_id']))['rows']
                    if [output['clip_id'] for output in outputs] != members:
                        raise ValueError('Mapping WAV sau kiểm tra không khớp nhóm.')
                    record['checked'][key] = {'audit_id': identifier, 'rows': outputs}
                    if all(output.get('processed', {}).get('state') == 'ready_for_review' for output in outputs):
                        for output in outputs:
                            member_key = key + ':' + output['clip_id'] if len(members) > 1 else key
                            record['checked'][member_key] = {'audit_id': identifier, 'row': output}
                            record['solutions'][output['clip_id']] = member_key
                except Exception as exc:
                    budget.check(cancel)
                    check_current()
                    record['checked'][key] = {'audit_id': identifier, 'error': str(exc)[:500]}
                checkpoint()

        try:
            budget.check(cancel)
            check_current()
            record['state'] = 'running'
            # A crash can occur after the worker committed results but before this checkpoint.
            # Recover only receipts owned by this exact ledger, never another run with the same text.
            receipts = set(budget.snapshot()['receipts'])
            known = {generated['attempt_id'] for generated in record['generated']}
            if receipts:
                for committed in (store.owner_root(owner) / 'repair-work').glob('*/results.json'):
                    attempts = read_json(committed.parent / 'attempts.json')
                    if attempts['receipt_id'] not in receipts or attempts['input_binding'] != binding:
                        continue
                    for generated in read_json(committed):
                        if generated['attempt_id'] not in known:
                            record['generated'].append(generated)
                            known.add(generated['attempt_id'])
            checkpoint()
            # First recover already generated candidates; never blindly re-run TTS on resume.
            evaluate_pending()
            context_attempts = record.setdefault('context_attempts', [])
            for item in record['queue']:
                if (item['mode'] != 'shorten' or item['id'] in record['solutions']
                        or item['id'] in context_attempts or len(context_attempts) >= 2):
                    continue
                original_row = source_rows[item['id']]
                speech = original_row['dubbed_audio']['speech_evidence']
                if speech['end_ms'] - speech['start_ms'] > 1.15 * (item['source_end_ms'] - item['source_start_ms'] + 250):
                    continue  # Even the maximum permitted pause cannot fit this utterance.
                context_attempts.append(item['id'])
                checkpoint()
                try:
                    observed_context = context(service, budget, video, media, [item],
                        cache_dir=store.owner_root(owner) / 'source-context', cancel=cancel)
                    identifier = uuid.uuid4().hex
                    inspect(store, owner, identifier, video, media, voice, subtitles, [item['id']],
                        align_source=False, model_dir=model_dir, cancel=cancel, progress=progress,
                        timeout_seconds=min(600, budget.remaining_seconds()),
                        repair={'kind': 'borrow', 'row': original_row, 'context': observed_context})
                    output = read_json(audit_path(store, owner, identifier))['rows'][0]
                    key = 'borrow:' + item['id']
                    record['checked'][key] = {'audit_id': identifier, 'row': output}
                    if output.get('processed', {}).get('state') == 'ready_for_review':
                        record['solutions'][item['id']] = key
                except Exception as exc:
                    budget.check(cancel)
                    check_current()
                    record['errors'].append({'stage': 'source_context', 'message': str(exc)[:500]})
                checkpoint()
            group_attempts = record.setdefault('group_attempts', [])
            for items in group_candidates(voice, subtitles, original_audit):
                members = [item['id'] for item in items]
                counters = budget.snapshot()
                if (members in group_attempts or any(member in record['solutions'] for member in members)
                        or counters['tts_limit'] - counters['tts_used'] < len(members)
                        or any(counters['per_cluster'].get(member, 0) >= 2 for member in members)):
                    continue
                group_attempts.append(members)
                checkpoint()
                try:
                    observed_context = context(service, budget, video, media, items,
                        cache_dir=store.owner_root(owner) / 'source-context', cancel=cancel)
                    decision = observed_context['decision']
                    if not (decision['source_is_clear'] and decision['same_speaking_turn']
                            and decision['fragmented_clauses'] and decision['confidence'] >= .9):
                        record['errors'].append({'stage': 'group', 'message': 'Giữ các vế riêng vì ngữ cảnh chưa cho phép ghép.'})
                        checkpoint()
                        continue
                    grouped = {'clip_id': members[0], 'member_ids': members,
                               'spoken_text': ' '.join(clips[member].spoken_text for member in members),
                               'mode': 'group', 'context': observed_context}
                    generated = generate(manager, owner, voice, budget, [grouped], device=device,
                        cancel=cancel, check_current=check_current)
                    record['generated'].extend(generated)
                    checkpoint()
                    evaluate_pending()
                except Exception as exc:
                    budget.check(cancel)
                    check_current()
                    record['errors'].append({'stage': 'group', 'message': str(exc)[:500]})
                checkpoint()
            shorten = [item for item in record['queue'] if item['mode'] == 'shorten'
                       and item['id'] not in record['wording'] and item['id'] not in record['solutions']]
            remaining_tts = budget.snapshot()['tts_limit'] - budget.snapshot()['tts_used']
            for index in range(0, min(len(shorten), remaining_tts), 8):
                batch = shorten[index:min(index + 8, remaining_tts)]
                budget.check(cancel)
                if progress:
                    progress(10, 'repair_text', 'Đang đối chiếu phương án lời đọc ngắn hơn')
                try:
                    reviewed = propose(service, budget, batch, cancel=cancel)
                    record['wording'].update(reviewed)
                except RepairBudgetExceeded:
                    raise
                except Exception as exc:
                    budget.check(cancel)
                    record['errors'].append({'stage': 'wording', 'message': str(exc)[:500]})
                    record['wording'].update({item['id']: [] for item in batch})
                checkpoint()
            while True:
                budget.check(cancel)
                check_current()
                counters = budget.snapshot()
                remaining = counters['tts_limit'] - counters['tts_used']
                batch = []
                for item in record['queue']:
                    if item['id'] in record['solutions']:
                        continue
                    used = counters['per_cluster'].get(item['id'], 0)
                    if used >= 2:
                        continue
                    if item['mode'] == 'retry_original':
                        candidate = {'clip_id': item['id'], 'spoken_text': item['spoken_text']}
                    else:
                        variants = record['wording'].get(item['id'], [])
                        if used >= len(variants):
                            continue
                        candidate = variants[used]
                    if len(batch) < remaining:
                        batch.append({**candidate, 'mode': item['mode']})
                if not batch:
                    break
                if progress:
                    progress(30, 'repair_tts', f'Đang tạo {len(batch)} phương án giọng cần sửa')
                generated = generate(manager, owner, voice, budget, batch, device=device,
                    cancel=cancel, check_current=check_current)
                record['generated'].extend(generated)
                checkpoint()
                evaluate_pending()
            record['state'] = 'finalizing'
        except RepairBudgetExceeded as exc:
            record.update(state='finalizing', stop_reason=str(exc))
        except BaseException as exc:
            record.update(state='canceled' if cancel.is_set() else 'failed',
                          stop_reason=stale[0] if stale else str(exc)[:500])
            raise
        finally:
            stopped.set()
            watcher.join(timeout=2)
            checkpoint()
        # Preserve all cheap solutions and replace only rows whose new WAV passed every check.
        rows = [record['checked'][record['solutions'][row['clip_id']]]['row']
                if row['clip_id'] in record['solutions'] else row for row in original_audit['rows']]
        result_audit = {**original_audit, 'id': record['result_audit_id'], 'rows': rows,
                        'repair_run': audit_id, 'repair_budget': record['budget'], 'automatic_apply': False}
        write_json(audit_path(store, owner, record['result_audit_id']), result_audit)
        record['state'] = 'succeeded'
        checkpoint()
        return {'sync_audit_id': record['result_audit_id'], 'repair': record}
