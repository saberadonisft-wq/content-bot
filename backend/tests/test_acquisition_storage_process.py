"""Exercise acquisition storage using separate Windows-spawned interpreters."""

import multiprocessing
from pathlib import Path

import pytest

from app.sqlite_store import SQLiteStore

NOW = "2026-09-23T10:00:00+00:00"
EXPIRY = "2026-09-23T10:05:00+00:00"
LATER = "2026-09-23T10:06:00+00:00"


def _race_claim(path, operation, identity, barrier, results):
    store = SQLiteStore(Path(path))
    barrier.wait(timeout=20)
    if operation == "selection":
        result = store.reserve_acquisition_selection({
            "_id": identity, "idempotency_key": "shared-key", "state": "queued",
        })
    else:
        method = (
            store.claim_acquisition_channel
            if operation == "schedule"
            else store.claim_acquisition_reconciliation
        )
        result = method(
            "channel", run_id=identity if operation == "schedule" else "run",
            now=NOW, lease_expires_at=EXPIRY,
        )
    results.put(result)


def _stale_finalizer(path, claimed_event, finish_event, results):
    store = SQLiteStore(Path(path))
    claim = store.claim_acquisition_reconciliation(
        "channel", run_id="run", now=NOW, lease_expires_at=EXPIRY,
    )
    results.put(claim)
    claimed_event.set()
    if not finish_event.wait(20):
        raise TimeoutError("Parent did not finish lease takeover")
    results.put(store.complete_acquisition_reconciliation(
        "channel", run_id="run",
        lease_token=claim["subscription"]["reconcile_lease_token"],
        now=LATER,
        updates={"active_run_id": None, "last_status": "stale-owner"},
    ))


def _stop_owned_processes(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)


@pytest.mark.parametrize("operation", ["schedule", "reconciliation", "selection"])
def test_acquisition_reservation_has_one_winner_across_processes(tmp_path, operation):
    store = SQLiteStore(tmp_path / "race.db")
    store.initialize()
    store.upsert_acquisition_document("acquisition_channels", {
        "_id": "channel",
        "subscription": {
            "enabled": True, "next_run_at": NOW,
            "active_run_id": "run" if operation == "reconciliation" else None,
        },
    })
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [context.Process(
        target=_race_claim,
        args=(str(store.path), operation, f"worker-{index}", barrier, results),
    ) for index in range(2)]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=20)
        rows = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        if operation == "selection":
            assert rows[0]["id"] == rows[1]["id"]
            assert len(store.acquisition_documents("acquisition_selections")) == 1
        else:
            winners = [row for row in rows if row is not None]
            assert len(winners) == 1
            saved = store.acquisition_document("acquisition_channels", "channel")
            assert saved["subscription"] == winners[0]["subscription"]
    finally:
        _stop_owned_processes(processes)
        results.close()
        results.join_thread()


def test_stale_process_cannot_complete_reassigned_reconciliation(tmp_path):
    store = SQLiteStore(tmp_path / "takeover.db")
    store.initialize()
    store.upsert_acquisition_document("acquisition_channels", {
        "_id": "channel", "subscription": {"enabled": True, "active_run_id": "run"},
    })
    context = multiprocessing.get_context("spawn")
    claimed_event, finish_event = context.Event(), context.Event()
    results = context.Queue()
    process = context.Process(
        target=_stale_finalizer,
        args=(str(store.path), claimed_event, finish_event, results),
    )
    process.start()
    try:
        assert claimed_event.wait(20)
        old_claim = results.get(timeout=10)
        new_claim = store.claim_acquisition_reconciliation(
            "channel", run_id="run", now=EXPIRY,
            lease_expires_at="2026-09-23T10:10:00+00:00",
        )
        token = new_claim["subscription"]["reconcile_lease_token"]
        assert token != old_claim["subscription"]["reconcile_lease_token"]
        finish_event.set()
        assert results.get(timeout=20) is None
        process.join(timeout=10)
        assert process.exitcode == 0
        assert store.acquisition_document("acquisition_channels", "channel") == new_claim
        completed = store.complete_acquisition_reconciliation(
            "channel", run_id="run", lease_token=token, now=LATER,
            updates={"active_run_id": None, "reconcile_run_id": None,
                     "reconcile_lease_token": None, "last_status": "succeeded"},
        )
        assert completed["subscription"]["last_status"] == "succeeded"
    finally:
        finish_event.set()
        _stop_owned_processes([process])
        results.close()
        results.join_thread()
