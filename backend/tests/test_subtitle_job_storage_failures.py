"""Faults at real job JSON writes must not leave phantom/running jobs."""

import errno
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from test_subtitle_jobs import _wait_for_state

from app.services.subtitle_jobs import (
    SubtitleJobManager,
    SubtitleJobQueueFull,
    SubtitleJobRecord,
)


def fail_job_writes(monkeypatch, root, predicate):
    original = Path.write_text

    def write(path, data, *args, **kwargs):
        if path.parent == root and path.name.endswith('.json.part') and predicate(json.loads(data)):
            raise OSError(errno.ENOSPC, 'fixture job disk full')
        return original(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)


def test_submit_write_failure_does_not_admit_a_phantom_job(tmp_path, monkeypatch):
    full = True
    fail_job_writes(monkeypatch, tmp_path, lambda _: full)
    manager = SubtitleJobManager(tmp_path)
    calls = []
    try:
        with pytest.raises(OSError, match='disk full'):
            manager.submit('translation', 'same', lambda _: calls.append(1) or {})
        full = False
        retry = manager.submit('translation', 'same', lambda _: calls.append(1) or {})
        assert _wait_for_state(manager, retry['id'], {'succeeded'})
        assert calls == [1]
    finally:
        manager.shutdown()


@pytest.mark.parametrize('stage', ['starting', 'complete', 'progress'])
def test_worker_write_failure_stays_pollable_until_saved(tmp_path, monkeypatch, stage):
    full = True
    allowed = {'queued'} if stage == 'starting' else {'queued', 'starting'}
    fail_job_writes(monkeypatch, tmp_path, lambda row: full and row['phase'] not in allowed)
    manager = SubtitleJobManager(tmp_path, max_cached=0)
    calls = []

    def run(context):
        calls.append(1)
        if stage == 'progress':
            manager._last_persist_at[context.job_id] = 0
            context.update(30, 'reading', 'Reading media')
        return {'value': 7}

    try:
        job = manager.submit('translation', 'same', run)
        failed = _wait_for_state(manager, job['id'], {'failed'})
        manager.shutdown()
        assert manager.get(job['id'])['state'] == 'failed'
        assert failed['details']['state_persistence_error']
        assert calls == ([] if stage == 'starting' else [1])
        assert not manager._futures and not manager._cancel_events
        full = False
        repaired = manager.get(job['id'])
        assert repaired['state'] == 'failed'
        assert 'state_persistence_error' not in repaired['details']
        assert json.loads((tmp_path / f"{job['id']}.json").read_text(encoding='utf-8'))['state'] == 'failed'
    finally:
        manager.shutdown()
    restored = SubtitleJobManager(tmp_path)
    try:
        assert restored.get(job['id'])['state'] == 'failed'
        retried = restored.submit('translation', 'same', lambda _: {'retried': True})
        assert retried['id'] != job['id']
        assert _wait_for_state(restored, retried['id'], {'succeeded'})['result'] == {'retried': True}
    finally:
        restored.shutdown()


def test_disk_full_does_not_prevent_cancel_or_shutdown(tmp_path, monkeypatch):
    full = False
    fail_job_writes(monkeypatch, tmp_path, lambda _: full)
    manager = SubtitleJobManager(tmp_path, max_workers=1, max_cached=0)
    entered = threading.Event()

    def run(context):
        entered.set()
        assert context.cancel_event.wait(3)
        context.raise_if_canceled()

    try:
        active = manager.submit('translation', 'active', run)
        assert entered.wait(2)
        queued = manager.submit('translation', 'queued', run)
        full = True
        assert manager.cancel(queued['id'])['state'] == 'canceled'
        manager.shutdown(timeout_seconds=2)
        for job in (active, queued):
            assert manager.get(job['id'])['state'] == 'canceled'
        assert not manager._futures and not manager._cancel_events
        full = False
        for job in (active, queued):
            manager.get(job['id'])
            assert json.loads((tmp_path / f"{job['id']}.json").read_text(encoding='utf-8'))['state'] == 'canceled'
    finally:
        full = False
        manager.shutdown()


def test_recoverable_shutdown_still_signals_workers_when_writes_fail(tmp_path, monkeypatch):
    full = False
    fail_job_writes(monkeypatch, tmp_path, lambda _: full)
    manager = SubtitleJobManager(tmp_path, recoverable_kinds=('generation',))
    entered = threading.Event()

    def run(context):
        context.update_details({'completed': 2})
        entered.set()
        assert context.cancel_event.wait(3)
        context.raise_if_canceled()

    try:
        job = manager.submit('generation', 'recover', run)
        assert entered.wait(2)
        full = True
        manager.shutdown(timeout_seconds=2)
        saved = manager.get(job['id'])
        assert saved['phase'] == 'interrupted' and not saved['cancel_requested']
        assert saved['details']['completed'] == 2
        assert saved['details']['state_persistence_error']
        assert not manager._futures and not manager._cancel_events
        full = False
        assert 'state_persistence_error' not in manager.get(job['id'])['details']
    finally:
        full = False
        manager.shutdown()
    restored = SubtitleJobManager(tmp_path, recoverable_kinds=('generation',))
    try:
        restored.recover_interrupted(lambda _: lambda context: {'recovered': True})
        assert _wait_for_state(restored, job['id'], {'succeeded'})['result']['recovered']
    finally:
        restored.shutdown()


def test_recovery_write_failure_does_not_abort_startup_or_admit_work(tmp_path, monkeypatch):
    job = SubtitleJobRecord(id='a' * 20, kind='generation', dedupe_key='recover', state='running')
    (tmp_path / f'{job.id}.json').write_text(json.dumps(job.snapshot()), encoding='utf-8')
    full = True
    fail_job_writes(monkeypatch, tmp_path, lambda _: full)
    manager = SubtitleJobManager(tmp_path, max_cached=0, recoverable_kinds=('generation',))
    calls = []
    factory = lambda _: lambda context: calls.append(context.job_id) or {}
    try:
        manager.recover_interrupted(factory)
        assert manager.get(job.id)['phase'] == 'interrupted'
        assert not calls and not manager._futures and not manager._dedupe
        full = False
        manager.recover_interrupted(factory)
        _wait_for_state(manager, job.id, {'succeeded'})
        assert calls == [job.id]
    finally:
        full = False
        manager.shutdown()


@pytest.mark.parametrize('failure', ['corrupt', 'insert'])
def test_failed_index_keeps_saved_result_and_deduplication(tmp_path, monkeypatch, failure):
    if failure == 'corrupt':
        (tmp_path / '.dedupe.sqlite3').write_bytes(b'corrupt database')
    manager = SubtitleJobManager(tmp_path, max_cached=0)
    if failure == 'insert':
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('database or disk is full')
        monkeypatch.setattr(manager, '_write_dedupe', fail)
    try:
        job = manager.submit('translation', 'paid-work', lambda _: {'value': 7})
        _wait_for_state(manager, job['id'], {'succeeded'})
        assert manager.submit('translation', 'paid-work', lambda _: pytest.fail('Repeated paid work'))['id'] == job['id']
        manager.shutdown()
        assert not manager._records
        # Fresh manager must also work with corrupt/missing index entries.
        restored = SubtitleJobManager(tmp_path, max_cached=0)
        try:
            actual = restored.submit('translation', 'paid-work', lambda _: pytest.fail('Repeated paid work'))
            assert actual['id'] == job['id'] and actual['result'] == {'value': 7}
        finally:
            restored.shutdown()
    finally:
        manager.shutdown()


@pytest.mark.parametrize('error_number, status', [(errno.ENOSPC, 507), (errno.EACCES, 503)])
def test_job_admission_returns_actionable_http_error(tmp_path, monkeypatch, error_number, status):
    from fastapi.testclient import TestClient

    from app.main import create_app, settings

    manager = SubtitleJobManager(tmp_path)
    application = create_app(scheduler=False)
    monkeypatch.setattr(settings, 'content_bot_auth_enabled', False)

    @application.post('/fixture-job')
    def submit():
        return manager.submit('translation', 'same', lambda _: {})

    def fail(record):
        raise OSError(error_number, 'fixture secret directory')

    monkeypatch.setattr(manager, '_write_record', fail)
    client = TestClient(application)
    try:
        response = client.post('/fixture-job')
        assert response.status_code == status
        assert 'thử lại' in response.json()['detail']
        assert 'secret directory' not in response.text
        assert not manager._records and not manager._futures and not manager._dedupe
    finally:
        client.close()
        manager.shutdown()


def test_pending_unsaved_states_limit_admission_and_repair_on_retry(tmp_path, monkeypatch):
    full = True
    fail_job_writes(monkeypatch, tmp_path, lambda row: full and row['phase'] != 'queued')
    manager = SubtitleJobManager(tmp_path, max_cached=0, max_pending=1)
    try:
        job = manager.submit('translation', 'one', lambda _: {})
        _wait_for_state(manager, job['id'], {'failed'})
        with pytest.raises(SubtitleJobQueueFull):
            manager.submit('translation', 'two', lambda _: {})
        full = False
        retry = manager.submit('translation', 'one', lambda _: {'retried': True})
        assert retry['id'] != job['id']
        _wait_for_state(manager, retry['id'], {'succeeded'})
        assert manager.get(job['id'])['state'] == 'failed'
        assert not manager._unpersisted
    finally:
        full = False
        manager.shutdown()


def test_replace_failure_preserves_complete_json_and_removes_partial_file(tmp_path, monkeypatch):
    manager = SubtitleJobManager(tmp_path, max_cached=0)
    original = Path.replace
    full = True

    def replace(path, target):
        if (full and path.parent == tmp_path and path.name.endswith('.json.part')
                and json.loads(path.read_text(encoding='utf-8'))['phase'] != 'queued'):
            raise OSError(errno.ENOSPC, 'fixture replace failed')
        return original(path, target)

    monkeypatch.setattr(Path, 'replace', replace)
    try:
        job = manager.submit('translation', 'replace', lambda _: pytest.fail('Must not run'))
        failed = _wait_for_state(manager, job['id'], {'failed'})
        assert failed['details']['state_persistence_error']
        path = tmp_path / f"{job['id']}.json"
        assert json.loads(path.read_text(encoding='utf-8'))['state'] == 'queued'
        assert not list(tmp_path.glob('*.part'))
        full = False
        assert manager.get(job['id'])['state'] == 'failed'
        assert json.loads(path.read_text(encoding='utf-8'))['state'] == 'failed'
    finally:
        full = False
        manager.shutdown()
