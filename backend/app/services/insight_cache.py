"""Bounded derived results, invalidated by the database revision (also across processes)."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from time import monotonic
from weakref import WeakKeyDictionary

from .item_query import ANALYSIS_VERSION, ItemQuery

_TTL_SECONDS = 30
_MAX_ENTRIES = 32
_MAX_BYTES = 8 * 1024 * 1024
_lock = Lock()
_stores: WeakKeyDictionary = WeakKeyDictionary()


def cached_aggregate(store, query: ItemQuery, options: tuple, compute: Callable[[], dict]) -> dict:
    revision_fn = getattr(store, "item_query_revision", None)
    if revision_fn is None:
        return compute()
    revision = revision_fn()
    key = (revision, ANALYSIS_VERSION, query, options)
    now = monotonic()
    with _lock:
        cache = _stores.setdefault(store, OrderedDict())
        # A new revision makes all prior results obsolete; don't retain large
        # cluster responses while ingestion is active.
        for old_key, (expires, _) in list(cache.items()):
            if old_key[0] != revision or expires <= now:
                del cache[old_key]
        hit = cache.get(key)
        if hit:
            cache.move_to_end(key)
            return json.loads(hit[1])
    result = compute()
    if revision_fn() == revision:
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) <= _MAX_BYTES:
            with _lock:
                cache[key] = (monotonic() + _TTL_SECONDS, encoded)
                while len(cache) > _MAX_ENTRIES or sum(len(value[1]) for value in cache.values()) > _MAX_BYTES:
                    cache.popitem(last=False)
    return result
