"""Durable, pre-dispatch accounting for bounded dubbing repair."""
from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from .audio_cache import database_lock
from .store import read_json, write_json


class RepairBudgetExceeded(RuntimeError):
    pass


class RepairBudget:
    def __init__(self, path: Path, *, input_binding: str, cluster_ids: list[str]):
        if not cluster_ids or len(set(cluster_ids)) != len(cluster_ids) or len(cluster_ids) > 20000:
            raise ValueError('Danh sách cụm sửa không hợp lệ.')
        if not re.fullmatch(r'[a-f0-9]{64}', input_binding):
            raise ValueError('Thiếu dấu kiểm tra đầu vào sửa giọng.')
        self.path = path
        self.lock_path = path.with_suffix('.lock.sqlite3')
        with database_lock(self.lock_path):
            if path.exists():
                record = read_json(path)
                if record['input_binding'] != input_binding or record['cluster_ids'] != cluster_ids:
                    raise ValueError('Không dùng ngân sách cũ cho đầu vào mới.')
            else:
                started = time.time()
                record = {'schema_version': 1, 'input_binding': input_binding, 'cluster_ids': cluster_ids,
                    'started_at': started, 'deadline': started + 600,
                    'tts_limit': min(40, max(2, math.ceil(.1 * len(cluster_ids)))),
                    'tts_used': 0, 'gemini_used': 0, 'per_cluster': {}, 'receipts': {}}
                write_json(path, record)
        self.deadline = record['deadline']

    def check(self, cancel: threading.Event | None = None):
        if cancel and cancel.is_set():
            raise InterruptedError('Đã dừng sửa giọng.')
        if time.time() >= self.deadline:
            raise RepairBudgetExceeded('Đã hết ngân sách 10 phút sửa giọng; giữ kết quả đã có.')

    def remaining_seconds(self):
        return max(0, self.deadline - time.time())

    def snapshot(self):
        with database_lock(self.lock_path):
            return read_json(self.path)

    def reserve(self, kind: str, cluster_ids: list[str], *, receipt_id: str | None = None,
                cancel: threading.Event | None = None) -> str:
        """Commit before I/O. Interrupted/failed attempts are never refunded or replayed."""
        self.check(cancel)
        if kind not in {'tts', 'gemini'} or not cluster_ids:
            raise ValueError('Loại thao tác sửa không hợp lệ.')
        receipt_id = receipt_id or uuid.uuid4().hex
        if not re.fullmatch(r'[a-f0-9]{32}', receipt_id):
            raise ValueError('ID lượt thử không hợp lệ.')
        with database_lock(self.lock_path, cancel):
            self.check(cancel)
            record = read_json(self.path)
            if receipt_id in record['receipts']:
                raise ValueError('Lượt thử đã được tính; không phát lại sau gián đoạn.')
            if not set(cluster_ids) <= set(record['cluster_ids']):
                raise ValueError('Cụm không thuộc lượt sửa này.')
            if kind == 'gemini':
                if len(cluster_ids) > 8 or len(set(cluster_ids)) != len(cluster_ids):
                    raise ValueError('Mỗi request Gemini có tối đa 8 cụm khác nhau.')
                if record['gemini_used'] >= 8:
                    raise RepairBudgetExceeded('Đã dùng hết 8 request Gemini, kể cả thử lại.')
                record['gemini_used'] += 1
            else:
                counts = Counter(cluster_ids)
                if (record['tts_used'] + len(cluster_ids) > record['tts_limit']
                        or any(record['per_cluster'].get(key, 0) + count > 2 for key, count in counts.items())):
                    raise RepairBudgetExceeded('Đã hết lượt TTS bổ sung của cụm hoặc của toàn tác vụ.')
                record['tts_used'] += len(cluster_ids)
                for key, count in counts.items():
                    record['per_cluster'][key] = record['per_cluster'].get(key, 0) + count
            record['receipts'][receipt_id] = {'kind': kind, 'cluster_ids': cluster_ids,
                'state': 'reserved', 'reserved_at': time.time()}
            write_json(self.path, record)
        return receipt_id

    def settle(self, receipt_id: str, *, succeeded: bool, error: str | None = None):
        with database_lock(self.lock_path):
            record = read_json(self.path)
            receipt = record['receipts'][receipt_id]
            state = 'succeeded' if succeeded else 'failed'
            if receipt['state'] not in {'reserved', state}:
                raise ValueError('Không thay đổi kết quả lượt thử đã chốt.')
            receipt.update(state=state, finished_at=time.time(), error=error[:500] if error else None)
            write_json(self.path, record)
