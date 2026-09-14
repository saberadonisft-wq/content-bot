import time
from types import SimpleNamespace

from test_voice_repair_pipeline import setup_repair
from test_voice_sync_apply import candidate

from app.services.voiceover import sync_generation as module
from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.store import read_json, write_json
from app.services.voiceover.sync_apply import apply_sync_candidates
from app.services.voiceover.sync_audit import audit_path


def test_started_job_runs_sync_after_cached_tts_and_resume_retains_session(tmp_path, monkeypatch):
    store, doc, subtitles, _media, _identifier, _row = setup_repair(tmp_path)
    manager = VoiceManager(store)
    monkeypatch.setattr(manager, 'status', lambda: {'ready': True, 'devices': ['cpu']})
    calls = []

    def sync(active, owner, job, persist):
        assert job['state'] == 'running' and job['completed'] == 1
        assert read_json(store.owner_root(owner) / 'sync-generation' / job['sync_session_id'] / 'input.json')['document'] == subtitles
        calls.append(job['sync_session_id'])
        job['state'] = 'succeeded'
        persist()

    manager.sync_runner = sync
    try:
        job = manager.start('user', doc.project_id, 'cpu', subtitle_document=subtitles)
        deadline = time.monotonic() + 3
        while manager.live and time.monotonic() < deadline:
            time.sleep(.02)
        assert calls == [job['sync_session_id']]
        resumed = manager.control('user', job['id'], 'resume')
        deadline = time.monotonic() + 3
        while manager.live and time.monotonic() < deadline:
            time.sleep(.02)
        assert calls == [job['sync_session_id']] * 2 and resumed['sync_session_id'] == job['sync_session_id']
    finally:
        manager.shutdown()


def test_cached_generation_does_not_replace_processed_asset_with_raw_wave(tmp_path, monkeypatch):
    store, doc, identifier, _record, args = candidate(tmp_path)
    applied = apply_sync_candidates(store, 'user', identifier, **args)
    manager = VoiceManager(store)
    monkeypatch.setattr(manager, 'status', lambda: {'ready': True, 'devices': ['cpu']})
    try:
        manager.start('user', doc.project_id, 'cpu')
        deadline = time.monotonic() + 3
        while manager.live and time.monotonic() < deadline:
            time.sleep(.02)
        restored = store.get_document('user', doc.project_id)
        assert restored.clips[0].asset_id == applied.clips[0].asset_id
        assert restored.clips[0].sync.alignment == applied.clips[0].sync.alignment
    finally:
        manager.shutdown()


def test_coordinator_chains_audit_repair_apply_and_preserves_resume_counts(tmp_path, monkeypatch):
    store, doc, subtitles, media, _identifier, row = setup_repair(tmp_path)
    manager = VoiceManager(store)
    from app.api import media_paths
    monkeypatch.setattr(media_paths, '_uploaded_video_path', lambda _: tmp_path / 'input.mp4')
    monkeypatch.setattr(module, 'probe_media_cached', lambda *args: media)
    session = 'b' * 32
    root = store.owner_root('user') / 'sync-generation' / session
    write_json(root / 'input.json', {'document': subtitles, 'clip_ids': ['one']})
    job = {'id': 'c' * 20, 'project_id': doc.project_id, 'sync_session_id': session, 'device': 'cpu'}
    calls = []

    def audit(store, owner, identifier, *args, **options):
        calls.append('audit')
        assert options['timeout_seconds'] <= 600 and options['align_source']
        write_json(audit_path(store, owner, identifier), {'rows': [row], 'warnings': []})

    def repair(manager, service, owner, identifier, *args, **options):
        calls.append('repair')
        assert options['evaluated_clip_ids'] == ['one']
        result = read_json(audit_path(store, owner, identifier))
        result['rows'][0]['processed'] = {'state': 'ready_for_review'}
        write_json(audit_path(store, owner, identifier), result)
        return {'sync_audit_id': identifier}

    def apply(store, owner, identifier, **options):
        calls.append('apply')
        assert options['clip_ids'] == ['one'] and options['subtitles'] == subtitles
        updated = store.get_document(owner, doc.project_id)
        updated.clips[0].offset_ms += 1
        return store.save_document(owner, updated)

    try:
        options = {'settings': SimpleNamespace(data_dir=tmp_path, content_bot_alignment_whisper_model='small'),
                   'audit': audit, 'repair': repair, 'apply': apply}
        module.finish_generation_sync(manager, None, 'user', job, lambda: None, **options)
        assert calls == ['audit', 'repair', 'apply']
        assert job['state'] == 'succeeded' and job['sync_result']['applied'] == 1
        module.finish_generation_sync(manager, None, 'user', job, lambda: None, **options)
        assert calls == ['audit', 'repair', 'apply']  # Already applied, no repeated inference or offset.
    finally:
        manager.shutdown()


def test_api_start_and_resume_configure_real_sync_callback_without_work_on_read(tmp_path, monkeypatch):
    from app.api.voiceover import build_voiceover_router
    from app.services.voiceover.models import StartJob

    store, doc, subtitles, _media, _identifier, _row = setup_repair(tmp_path)
    manager = VoiceManager(store)
    monkeypatch.setattr(manager, 'status', lambda: {'ready': True, 'devices': ['cpu']})
    calls = []
    service = object()

    def finish(active, actual_service, owner, job, persist, **options):
        assert active is manager and actual_service is service and owner == 'user'
        calls.append(job['sync_session_id'])
        job['state'] = 'succeeded'
        persist()

    monkeypatch.setattr(module, 'finish_generation_sync', finish)
    router = build_voiceover_router(manager, sync_service_provider=lambda: service)
    start = next(route.endpoint for route in router.routes if route.path.endswith('/jobs') and 'POST' in route.methods)
    control = next(route.endpoint for route in router.routes if route.path.endswith('/jobs/{job_id}/{action}'))
    try:
        assert not manager.live and not calls
        job = start(StartJob(project_id=doc.project_id, device='cpu', subtitle_document=subtitles), 'user')
        deadline = time.monotonic() + 3
        while manager.live and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(calls) == 1
        manager.sync_runner = None  # A new API process reconstructs this callback on resume too.
        control(job['id'], 'resume', 'user')
        deadline = time.monotonic() + 3
        while manager.live and time.monotonic() < deadline:
            time.sleep(.02)
        assert calls == [job['sync_session_id']] * 2
    finally:
        manager.shutdown()


def test_failed_initial_worker_releases_before_bounded_repair_starts(tmp_path, monkeypatch):
    import sys

    from app.services.voiceover import manager as manager_module
    from app.services.voiceover.store import VoiceStore

    source_store, doc, subtitles, _media, _identifier, _row = setup_repair(tmp_path / 'fixture')
    store = VoiceStore(tmp_path / 'fresh')
    doc.clips[0].asset_id, doc.clips[0].duration_ms = None, 0
    doc.revision = 0
    doc = store.save_document('user', doc)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'worker.py').write_text('import sys; sys.exit(5)', encoding='utf-8')
    monkeypatch.setattr(manager_module, 'RUNTIME', runtime)
    manager = VoiceManager(store)
    monkeypatch.setattr(manager, 'python', lambda device: sys.executable)
    monkeypatch.setattr(manager, 'status', lambda: {'ready': True, 'devices': ['cpu']})
    calls = []

    def repair(active, owner, job, persist):
        worker = active._processes[job['id']]
        assert worker.poll() == 5
        assert job['phase'] == 'sync'
        calls.append(job['id'])
        job['state'] = 'succeeded'

    manager.sync_runner = repair
    try:
        job = manager.start('user', doc.project_id, 'cpu', subtitle_document=subtitles)
        deadline = time.monotonic() + 4
        while manager.live and time.monotonic() < deadline:
            time.sleep(.02)
        assert calls == [job['id']] and manager.get('user', job['id'])['state'] == 'succeeded'
        assert source_store.root != store.root
    finally:
        manager.shutdown()
