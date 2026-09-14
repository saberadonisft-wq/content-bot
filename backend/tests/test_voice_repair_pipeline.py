import threading
from pathlib import Path

import pytest
from test_voice_sync_audit import setup

from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.repair_pipeline import run_sync_repair
from app.services.voiceover.store import read_json, write_json
from app.services.voiceover.sync_audit import audit_path, snapshot_binding


def setup_repair(tmp_path):
    store, doc, subtitles, media = setup(tmp_path)
    identifier = 'a' * 32
    row = {'clip_id': 'one', 'completed': True, 'issues': [],
           'source_evidence': {'method': 'asr_observed', 'transcript_complete': True,
                               'start_ms': 1100, 'end_ms': 1700},
           'dubbed_audio': {'issues': ['dubbed_speech_missing']}}
    write_json(audit_path(store, 'user', identifier), {'id': identifier, 'project_id': doc.project_id,
        'state': 'succeeded', 'clip_ids': ['one'], 'rows': [row], 'warnings': [],
        'source_fingerprint': media['fingerprint'], 'input_binding': snapshot_binding(doc, subtitles)})
    return store, doc, subtitles, media, identifier, row


@pytest.mark.parametrize('valid', [True, False])
def test_retry_measures_before_accepting_and_restart_never_repeats_tts(tmp_path, valid):
    store, doc, subtitles, media, identifier, row = setup_repair(tmp_path)
    manager = VoiceManager(store)
    before = store.path('user', 'projects', doc.project_id).read_bytes()
    calls = []

    def generate(manager, owner, voice, budget, candidates, **kwargs):
        receipt = budget.reserve('tts', [item['clip_id'] for item in candidates])
        budget.settle(receipt, succeeded=True)
        calls.extend(candidates)
        return [{**item, 'attempt_id': str(len(calls)) * 64, 'state': 'generated', 'asset': {'id': 'f' * 64}}
                for item in candidates]

    def inspect(store, owner, identifier, video, media, voice, subtitles, ids, **kwargs):
        assert kwargs['repair']['attempt']['spoken_text'] == doc.clips[0].spoken_text
        assert kwargs['timeout_seconds'] <= 600
        write_json(audit_path(store, owner, identifier), {'rows': [{**row,
            'processed': {'state': 'ready_for_review' if valid else 'needs_review'}}]})
        return {'sync_audit_id': identifier}

    try:
        args = (manager, None, 'user', identifier, Path('unused'), media, subtitles)
        opts = {'device': 'cpu', 'model_dir': tmp_path, 'generate': generate, 'inspect': inspect}
        result = run_sync_repair(*args, **opts)
        assert len(calls) == (1 if valid else 2)
        assert bool(result['repair']['solutions']) == valid
        assert result['repair']['budget']['tts_used'] == len(calls)
        assert result['repair']['budget']['gemini_used'] == 0
        restored = run_sync_repair(*args, **opts)
        assert restored['sync_audit_id'] == result['sync_audit_id'] and len(calls) == (1 if valid else 2)
        assert store.path('user', 'projects', doc.project_id).read_bytes() == before
    finally:
        manager.shutdown()


def test_resume_inspects_saved_wave_instead_of_regenerating_after_cancel(tmp_path):
    store, _doc, subtitles, media, identifier, row = setup_repair(tmp_path)
    manager = VoiceManager(store)
    cancel = threading.Event()
    generated = []

    def generate(manager, owner, voice, budget, candidates, **kwargs):
        receipt = budget.reserve('tts', ['one'])
        budget.settle(receipt, succeeded=True)
        generated.extend(candidates)
        cancel.set()
        return [{**candidates[0], 'attempt_id': 'f' * 64, 'state': 'generated', 'asset': {'id': 'e' * 64}}]

    def inspect(store, owner, identifier, *args, **kwargs):
        write_json(audit_path(store, owner, identifier), {'rows': [{**row, 'processed': {'state': 'ready_for_review'}}]})
        return {'sync_audit_id': identifier}

    try:
        args = (manager, None, 'user', identifier, Path('unused'), media, subtitles)
        opts = {'device': 'cpu', 'model_dir': tmp_path, 'cancel': cancel, 'generate': generate, 'inspect': inspect}
        with pytest.raises(InterruptedError):
            run_sync_repair(*args, **opts)
        checkpoint = read_json(store.owner_root('user') / 'repair-runs' / identifier / 'checkpoint.json')
        assert checkpoint['state'] == 'canceled' and len(checkpoint['generated']) == 1
        cancel.clear()
        result = run_sync_repair(*args, **opts)
        assert len(generated) == 1 and result['repair']['solutions'] == {'one': 'f' * 64}
    finally:
        manager.shutdown()


def test_verified_pause_is_tried_before_rewording_or_tts(tmp_path):
    store, doc, subtitles, media, identifier, row = setup_repair(tmp_path)
    doc.clips[0].spoken_text = 'Tôi muốn đi dạo cùng bạn.'
    from app.services.voiceover.store import generation_hash
    meta_path = store.path('user', 'assets', doc.clips[0].asset_id)
    meta = read_json(meta_path)
    meta['generation_hash'] = generation_hash(doc, doc.clips[0], meta['device'])
    write_json(meta_path, meta)
    doc = store.save_document('user', doc)
    row.update(proposal={'state': 'blocked', 'blocked_reasons': ['duration_not_feasible']},
        dubbed_audio={'speech_verified': True, 'speech_evidence': {'start_ms': 50, 'end_ms': 850}})
    record = read_json(audit_path(store, 'user', identifier))
    record.update(rows=[row], input_binding=snapshot_binding(doc, subtitles))
    write_json(audit_path(store, 'user', identifier), record)
    manager = VoiceManager(store)
    calls = []

    def context(service, budget, video, media, items, **options):
        calls.append('context')
        receipt = budget.reserve('gemini', [item['id'] for item in items])
        budget.settle(receipt, succeeded=True)
        return {'decision': {'fixture': True}}

    def inspect(store, owner, identifier, *args, **options):
        calls.append('inspect')
        assert options['repair']['kind'] == 'borrow'
        write_json(audit_path(store, owner, identifier), {'rows': [{**row,
            'processed': {'state': 'ready_for_review'}, 'pause_evidence': {'allowed_ms': 250}}]})
        return {'sync_audit_id': identifier}

    def forbidden(*args, **options):
        raise AssertionError('A verified pause solved the problem; no rewording or TTS is needed')

    try:
        result = run_sync_repair(manager, None, 'user', identifier, Path('unused'), media, subtitles,
            device='cpu', model_dir=tmp_path, context=context, inspect=inspect, propose=forbidden, generate=forbidden)
        assert calls == ['context', 'inspect']
        assert result['repair']['solutions'] == {'one': 'borrow:one'}
        assert result['repair']['budget']['tts_used'] == 0
    finally:
        manager.shutdown()


def test_restart_recovers_committed_worker_results_without_dispatching_tts(tmp_path):
    from app.services.voiceover.repair_budget import RepairBudget

    store, doc, subtitles, media, identifier, row = setup_repair(tmp_path)
    root = store.owner_root('user') / 'repair-runs' / identifier
    binding = snapshot_binding(doc, subtitles)
    budget = RepairBudget(root / 'budget.json', input_binding=binding, cluster_ids=['one'])
    receipt = budget.reserve('tts', ['one'])
    budget.settle(receipt, succeeded=True)
    work = store.owner_root('user') / 'repair-work' / ('c' * 32)
    write_json(work / 'attempts.json', {'receipt_id': receipt, 'input_binding': binding})
    write_json(work / 'results.json', [{'clip_id': 'one', 'mode': 'retry_original', 'spoken_text': doc.clips[0].spoken_text,
                                      'attempt_id': 'd' * 64, 'state': 'generated', 'asset': {'id': 'e' * 64}}])
    manager = VoiceManager(store)

    def inspect(store, owner, identifier, *args, **kwargs):
        write_json(audit_path(store, owner, identifier), {'rows': [{**row, 'processed': {'state': 'ready_for_review'}}]})
        return {'sync_audit_id': identifier}

    def forbidden(*args, **kwargs):
        raise AssertionError('Committed TTS result must be recovered, not generated again')

    try:
        result = run_sync_repair(manager, None, 'user', identifier, Path('unused'), media, subtitles,
            device='cpu', model_dir=tmp_path, generate=forbidden, inspect=inspect)
        assert result['repair']['budget']['tts_used'] == 1
        assert result['repair']['solutions'] == {'one': 'd' * 64}
    finally:
        manager.shutdown()
