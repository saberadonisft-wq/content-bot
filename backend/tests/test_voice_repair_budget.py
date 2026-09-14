import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.voiceover import repair_budget as module
from app.services.voiceover.store import read_json


def budget(tmp_path, count=40):
    return module.RepairBudget(tmp_path / 'budget.json', input_binding='a' * 64,
                               cluster_ids=[str(i) for i in range(count)])


@pytest.mark.parametrize(('count', 'limit'), [(1, 2), (40, 4), (99, 10), (1000, 40)])
def test_tts_limits_survive_restart_and_count_each_candidate(tmp_path, count, limit):
    first = budget(tmp_path, count)
    assert first.snapshot()['tts_limit'] == limit
    first.reserve('tts', ['0', '0'])
    resumed = budget(tmp_path, count)
    assert resumed.snapshot()['tts_used'] == 2
    with pytest.raises(module.RepairBudgetExceeded):
        resumed.reserve('tts', ['0'])
    for index in range(2, limit):
        resumed.reserve('tts', [str(index - 1)])
    with pytest.raises(module.RepairBudgetExceeded):
        resumed.reserve('tts', [str(min(count - 1, limit))])


def test_concurrent_callers_cannot_overspend_or_replay_unfinished_receipts(tmp_path):
    first = budget(tmp_path)

    def reserve(_):
        try:
            return budget(tmp_path).reserve('gemini', ['0'])
        except module.RepairBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        receipts = [value for value in pool.map(reserve, range(12)) if value]
    assert len(receipts) == 8 and first.snapshot()['gemini_used'] == 8
    with pytest.raises(ValueError, match='phát lại'):
        first.reserve('gemini', ['0'], receipt_id=receipts[0])
    first.settle(receipts[0], succeeded=False, error='503')
    assert first.snapshot()['gemini_used'] == 8


def test_deadline_and_input_binding_do_not_reset_after_restart(tmp_path, monkeypatch):
    first = budget(tmp_path)
    saved = read_json(first.path)
    monkeypatch.setattr(module.time, 'time', lambda: saved['deadline'] + 1)
    resumed = budget(tmp_path)
    with pytest.raises(module.RepairBudgetExceeded):
        resumed.reserve('tts', ['0'])
    with pytest.raises(ValueError, match='đầu vào mới'):
        module.RepairBudget(first.path, input_binding='b' * 64, cluster_ids=['0'])
    canceled = threading.Event()
    canceled.set()
    with pytest.raises(InterruptedError):
        resumed.reserve('gemini', ['0'], cancel=canceled)
