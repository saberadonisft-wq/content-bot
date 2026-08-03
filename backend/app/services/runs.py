from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from ..mongo import MongoStore, store
from .connectors import RawContentItem, SearchQuery, SourceConnector
from .text import clean_terms, engagement, percentile, recency_score, relevance


logger = logging.getLogger("content_bot.runs")


def utcnow() -> datetime:
    return datetime.now(UTC)


def next_scheduled_time(previous: datetime, interval_minutes: int, now: datetime) -> datetime:
    previous = previous if previous.tzinfo else previous.replace(tzinfo=UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    interval = timedelta(minutes=interval_minutes)
    if previous > now:
        return previous
    elapsed_intervals = int((now - previous) // interval) + 1
    return previous + elapsed_intervals * interval


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()

    async def publish(self, event: dict) -> None:
        for queue in tuple(self._subscribers):
            if not queue.full():
                queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            yield {"type": "connected", "at": utcnow().isoformat()}
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


class RunManager:
    def __init__(
        self,
        connectors: dict[str, SourceConnector],
        events: EventBus,
        storage: MongoStore | None = None,
    ) -> None:
        self.connectors = connectors
        self.events = events
        self.store = storage or store
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(3)
        self._browser_semaphore = asyncio.Semaphore(1)

    async def _store_call(self, method, *args, **kwargs):
        """Keep synchronous persistence calls off FastAPI's event loop."""
        return await asyncio.to_thread(method, *args, **kwargs)

    async def _set_source_progress(
        self,
        source_run_id: str,
        batch_id: str,
        *,
        phase: str,
        message: str,
        state: str | None = None,
        progress_mode: str = "determinate",
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_percent: float | None = None,
        browser_state: str | None = None,
    ) -> None:
        values = {
            "phase": phase,
            "message": message,
            "progress_mode": progress_mode,
            "heartbeat_at": utcnow(),
        }
        if progress_current is not None:
            values["progress_current"] = progress_current
        if progress_total is not None:
            values["progress_total"] = progress_total
        if progress_percent is not None:
            values["progress_percent"] = progress_percent
        if state is not None:
            values["state"] = state
        if browser_state is not None:
            values["browser_state"] = browser_state
        await self._store_call(self.store.update_source_run, source_run_id, values)
        await self.events.publish(
            {
                "type": "source-progress",
                "batch_id": batch_id,
                "source_run_id": source_run_id,
                "phase": phase,
                "state": state,
                "progress_mode": progress_mode,
                "progress_current": progress_current,
                "progress_total": progress_total,
                "progress_percent": progress_percent,
                "message": message,
                "browser_state": browser_state,
                "at": utcnow().isoformat(),
            }
        )

    async def start_batch(
        self, keyword_id: int, trigger: str = "manual", source_ids: list[str] | None = None
    ) -> str:
        keyword = await self._store_call(self.store.keyword, keyword_id)
        if not keyword:
            raise ValueError("Keyword not found")
        selected = source_ids or keyword.get("source_ids", []) or list(self.connectors)
        selected = [source_id for source_id in selected if source_id in self.connectors]
        if not selected:
            raise ValueError("No valid sources selected")
        active = await self._store_call(self.store.active_batch, keyword_id)
        if active:
            return active["id"]
        batch_id = str(uuid.uuid4())
        await self._store_call(
            self.store.create_batch,
            {
                "id": batch_id,
                "keyword_id": keyword_id,
                "trigger": trigger,
                "state": "queued",
                "started_at": None,
                "finished_at": None,
                "error_message": None,
            },
            [
                {
                    "id": str(uuid.uuid4()),
                    "batch_id": batch_id,
                    "source_id": source_id,
                    "state": "queued",
                    "checkpoint": {},
                    "fetched_count": 0,
                    "ingested_count": 0,
                    "phase": "queued",
                    "progress_mode": "determinate",
                    "progress_total": keyword.get("max_items_per_source", 500),
                    "message": "Waiting to start",
                    "browser_state": None,
                    "heartbeat_at": None,
                    "started_at": None,
                    "finished_at": None,
                    "error_message": None,
                }
                for source_id in selected
            ],
        )
        task = asyncio.create_task(self._execute_batch(batch_id), name=f"crawl-batch-{batch_id}")
        self._tasks[batch_id] = task
        return batch_id

    async def _execute_batch(self, batch_id: str) -> None:
        batch = await self._store_call(self.store.batch, batch_id)
        if not batch:
            return
        await self._store_call(
            self.store.update_batch, batch_id, {"state": "running", "started_at": utcnow()}
        )
        source_run_ids = [row["id"] for row in batch["source_runs"]]
        await self.events.publish({"type": "batch", "batch_id": batch_id, "state": "running"})
        cancel_requested = False
        try:
            await asyncio.gather(
                *(self._execute_source_run(source_run_id) for source_run_id in source_run_ids),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            cancel_requested = True
        source_runs = await self._store_call(self.store.source_runs, batch_id)
        if cancel_requested:
            now = utcnow()
            for source_run in source_runs:
                if source_run["state"] in {"queued", "running"}:
                    await self._store_call(
                        self.store.update_source_run,
                        source_run["id"],
                        {"state": "cancelled", "phase": "cancelled", "finished_at": now, "heartbeat_at": now},
                    )
                    source_run["state"] = "cancelled"
        states = [row["state"] for row in source_runs]
        if states and all(state == "succeeded" for state in states):
            final_state = "succeeded"
        elif states and all(state == "skipped" for state in states):
            final_state = "skipped"
        elif any(state in {"succeeded", "skipped"} for state in states):
            final_state = "partial"
        elif states and all(state == "cancelled" for state in states):
            final_state = "cancelled"
        else:
            final_state = "failed"
        await self._store_call(
            self.store.update_batch, batch_id, {"state": final_state, "finished_at": utcnow()}
        )
        await self.events.publish({"type": "batch", "batch_id": batch_id, "state": final_state})

    async def _execute_source_run(self, source_run_id: str) -> None:
        source_run = await self._store_call(self.store.source_run, source_run_id)
        if not source_run:
            return
        batch_id = source_run["batch_id"]
        batch = await self._store_call(self.store.batch, batch_id)
        keyword = await self._store_call(self.store.keyword, batch["keyword_id"]) if batch else None
        if not keyword:
            await self._set_source_progress(
                source_run_id,
                batch_id,
                phase="failed",
                state="failed",
                message="Keyword was deleted",
            )
            return
        connector = self.connectors[source_run["source_id"]]
        max_items = keyword.get("max_items_per_source", 500)
        if batch["trigger"] == "schedule" and connector.capabilities.requires_login:
            detail = "Scheduled runs never open login-gated sources. Run this source manually when you can complete login."
            now = utcnow()
            await self._store_call(
                self.store.update_source_run,
                source_run_id,
                {"state": "skipped", "phase": "skipped", "message": detail, "error_message": detail, "finished_at": now, "heartbeat_at": now},
            )
            await self.events.publish(
                {"type": "source-run", "batch_id": batch_id, "source_run_id": source_run_id, "state": "skipped", "detail": detail}
            )
            return
        await self._set_source_progress(
            source_run_id,
            batch_id,
            phase="checking_source",
            message="Checking source availability",
            progress_mode="indeterminate" if connector.capabilities.requires_login else "determinate",
            progress_total=max_items,
        )
        status = await connector.healthcheck()
        if status.state != "ready":
            now = utcnow()
            await self._store_call(
                self.store.update_source_run,
                source_run_id,
                {"state": "skipped", "phase": "skipped", "message": status.detail, "error_message": status.detail, "finished_at": now, "heartbeat_at": now},
            )
            await self.events.publish(
                {"type": "source-run", "batch_id": batch_id, "source_run_id": source_run_id, "state": "skipped", "detail": status.detail}
            )
            return
        started_at = utcnow()
        await self._store_call(
            self.store.update_source_run,
            source_run_id,
            {"state": "running", "phase": "starting", "started_at": started_at, "message": "Starting scan", "heartbeat_at": started_at},
        )
        query = SearchQuery(
            keyword_id=keyword["id"],
            name=keyword["name"],
            include_terms=clean_terms([keyword["name"], *keyword.get("include_terms", [])]),
            max_items=keyword.get("max_items_per_source", 500),
        )
        exclude_terms = clean_terms(keyword.get("exclude_terms", []))
        semaphore = self._browser_semaphore if connector.capabilities.requires_login else self._semaphore
        fetched_count = 0
        ingested_count = 0

        async def on_connector_progress(status: str, detail: str) -> None:
            current = await self._store_call(self.store.source_run, source_run_id)
            if not current or current.get("state") != "running":
                return
            if status == "retrying":
                await self._set_source_progress(
                    source_run_id,
                    batch_id,
                    phase="recovering_browser",
                    message=detail,
                    progress_mode="indeterminate",
                    progress_current=current.get("fetched_count", 0),
                    progress_total=max_items,
                    browser_state="reopening",
                )
                return
            if status != "authenticated":
                return
            await self._set_source_progress(
                source_run_id,
                batch_id,
                phase="authenticated",
                message=detail,
                progress_mode="determinate",
                progress_current=current.get("fetched_count", 0),
                progress_total=max_items,
                browser_state="authenticated",
            )

        query.progress_callback = (
            on_connector_progress if connector.capabilities.requires_login else None
        )
        try:
            if connector.capabilities.requires_login:
                await self._set_source_progress(
                    source_run_id,
                    batch_id,
                    phase="opening_browser",
                    message="Opening Cốc Cốc browser",
                    progress_mode="indeterminate",
                    progress_total=max_items,
                    browser_state="opening",
                )
                await self._set_source_progress(
                    source_run_id,
                    batch_id,
                    phase="waiting_login",
                    message="Complete QR or phone login in the Cốc Cốc window",
                    progress_mode="indeterminate",
                    progress_total=max_items,
                    browser_state="waiting_login",
                )
            else:
                await self._set_source_progress(
                    source_run_id,
                    batch_id,
                    phase="searching",
                    message="Searching public source",
                    progress_total=max_items,
                )
            async with semaphore:
                async for raw_item in connector.search(query):
                    fetched_count += 1
                    if connector.capabilities.requires_login and fetched_count == 1:
                        await self._set_source_progress(
                            source_run_id,
                            batch_id,
                            phase="scanning",
                            message="Login completed; scanning source results",
                            progress_mode="determinate",
                            progress_current=fetched_count,
                            progress_total=max_items,
                            progress_percent=round(min(fetched_count / max_items * 100, 99), 1),
                            browser_state="authenticated",
                        )
                    accepted = await self._ingest(source_run_id, keyword["id"], query.include_terms, exclude_terms, raw_item)
                    ingested_count += int(accepted)
                    await self.events.publish(
                        {
                            "type": "source-progress",
                            "batch_id": batch_id,
                            "source_run_id": source_run_id,
                            "phase": "processing",
                            "state": "running",
                            "progress_mode": "determinate",
                            "progress_current": fetched_count,
                            "progress_total": max_items,
                            "progress_percent": round(min(fetched_count / max_items * 100, 99), 1),
                            "fetched_count": fetched_count,
                            "ingested_count": ingested_count,
                            "message": f"Fetched {fetched_count}; stored {ingested_count}",
                            "at": utcnow().isoformat(),
                        }
                    )
            current = await self._store_call(self.store.source_run, source_run_id)
            if current and current["state"] == "running":
                finished_at = utcnow()
                await self._store_call(
                    self.store.update_source_run,
                    source_run_id,
                    {
                        "state": "succeeded",
                        "phase": "completed",
                        "message": f"Completed: fetched {fetched_count}; stored {ingested_count}",
                        "progress_mode": "determinate",
                        "progress_current": fetched_count,
                        "progress_total": max_items,
                        "progress_percent": 100.0,
                        "finished_at": finished_at,
                        "heartbeat_at": finished_at,
                        "browser_state": "closed" if connector.capabilities.requires_login else None,
                    },
                )
            await self.events.publish({"type": "source-run", "batch_id": batch_id, "source_run_id": source_run_id, "state": "succeeded"})
        except asyncio.CancelledError:
            now = utcnow()
            await self._store_call(
                self.store.update_source_run,
                source_run_id,
                {"state": "cancelled", "phase": "cancelled", "message": "Scan cancelled", "finished_at": now, "heartbeat_at": now},
            )
            raise
        except Exception as exc:
            raw_detail = str(exc)
            detail = raw_detail[-2000:]
            logger.exception("Source run failed: batch=%s source_run=%s", batch_id, source_run_id)
            browser_closed = "TargetClosedError" in raw_detail or "browser has been closed" in raw_detail.lower()
            now = utcnow()
            await self._store_call(
                self.store.update_source_run,
                source_run_id,
                {"state": "failed", "phase": "browser_closed" if browser_closed else "failed", "message": "Cốc Cốc was closed before the scan finished." if browser_closed else "Scan failed", "error_message": detail, "finished_at": now, "heartbeat_at": now, "browser_state": "closed" if browser_closed else None},
            )
            await self.events.publish(
                {"type": "source-run", "batch_id": batch_id, "source_run_id": source_run_id, "state": "failed", "phase": "browser_closed" if browser_closed else "failed", "detail": detail[-500:]}
            )

    async def _ingest(
        self,
        source_run_id: str,
        keyword_id: int,
        include_terms: list[str],
        exclude_terms: list[str],
        raw: RawContentItem,
    ) -> bool:
        accepted = await asyncio.to_thread(
            self._ingest_sync,
            source_run_id,
            keyword_id,
            include_terms,
            exclude_terms,
            raw,
        )
        if accepted:
            await self.events.publish(
                {"type": "item", "source_run_id": source_run_id, "title": raw.title[:160]}
            )
        return accepted

    def _ingest_sync(
        self,
        source_run_id: str,
        keyword_id: int,
        include_terms: list[str],
        exclude_terms: list[str],
        raw: RawContentItem,
    ) -> bool:
        source_run = self.store.source_run(source_run_id)
        if not source_run or source_run["state"] != "running":
            return False
        now = utcnow()
        self.store.update_source_run(
            source_run_id,
            {"heartbeat_at": now},
            {"fetched_count": 1, "progress_current": 1},
        )
        score, reasons = relevance(
            raw.title, raw.body_snippet, raw.hashtags, raw.author, include_terms, exclude_terms
        )
        item = self.store.item_by_source(source_run["source_id"], raw.external_id)
        if score <= 0:
            if item:
                self.store.delete_match(item["id"], keyword_id)
            return False
        values = {
            "source_id": source_run["source_id"],
            "external_id": raw.external_id,
            "canonical_url": raw.canonical_url,
            "title": raw.title,
            "body_snippet": raw.body_snippet,
            "author": raw.author,
            "hashtags": raw.hashtags,
            "locale": raw.locale,
            "published_at": raw.published_at,
            "metrics": raw.metrics,
            "raw_payload": json.loads(json.dumps(raw.raw_payload, ensure_ascii=False, default=str)),
            "first_seen_at": item["first_seen_at"] if item else now,
            "last_seen_at": now,
        }
        if item:
            values["id"] = item["id"]
        item = self.store.save_item(values)
        self.store.add_snapshot(
            {
                "content_item_id": item["id"],
                "captured_at": now,
                "view_count": int(raw.metrics.get("view_count", 0)),
                "like_count": int(raw.metrics.get("like_count", raw.metrics.get("reaction_count", 0))),
                "comment_count": int(raw.metrics.get("comment_count", 0)),
                "share_count": int(raw.metrics.get("share_count", 0)),
                "favorite_count": int(raw.metrics.get("favorite_count", 0)),
            }
        )
        self.store.save_match(
            item["id"],
            keyword_id,
            {
                "relevance_score": score,
                "trend_score": self._trend_score(item, score),
                "match_reasons": reasons,
                "updated_at": now,
            },
        )
        self.store.update_source_run(
            source_run_id,
            {"heartbeat_at": utcnow()},
            {"ingested_count": 1},
        )
        return True

    def _trend_score(self, item: dict, relevance_score: float) -> float:
        snapshots = self.store.snapshots(item["id"], descending=True, limit=2)
        current_engagement = engagement(item.get("metrics", {}))
        source_values = [engagement(value) for value in self.store.source_metric_values(item["source_id"])]
        engagement_rank = percentile(source_values, current_engagement)
        recency = recency_score(item.get("published_at"))
        relevance_part = relevance_score / 100
        if len(snapshots) == 2:
            latest, previous = snapshots
            latest_at = latest["captured_at"]
            previous_at = previous["captured_at"]
            elapsed = max((latest_at - previous_at).total_seconds() / 3600, 0.5)
            latest_value = latest.get("like_count", 0) + 2 * latest.get("comment_count", 0) + 3 * latest.get("share_count", 0) + 2 * latest.get("favorite_count", 0)
            previous_value = previous.get("like_count", 0) + 2 * previous.get("comment_count", 0) + 3 * previous.get("share_count", 0) + 2 * previous.get("favorite_count", 0)
            velocity = max((latest_value - previous_value) / elapsed, 0)
            velocities: list[float] = []
            for content_item_id in self.store.source_item_ids(item["source_id"]):
                pair = self.store.snapshots(content_item_id, descending=True, limit=2)
                if len(pair) != 2:
                    continue
                current, older = pair
                hours = max((current["captured_at"] - older["captured_at"]).total_seconds() / 3600, 0.5)
                current_value = current.get("like_count", 0) + 2 * current.get("comment_count", 0) + 3 * current.get("share_count", 0) + 2 * current.get("favorite_count", 0)
                older_value = older.get("like_count", 0) + 2 * older.get("comment_count", 0) + 3 * older.get("share_count", 0) + 2 * older.get("favorite_count", 0)
                velocities.append(max((current_value - older_value) / hours, 0))
            velocity_rank = percentile(velocities or [velocity], velocity)
            return round(100 * (0.45 * velocity_rank + 0.25 * engagement_rank + 0.20 * recency + 0.10 * relevance_part), 2)
        return round(100 * (0.55 * engagement_rank + 0.35 * recency + 0.10 * relevance_part), 2)

    async def scheduler_tick(self) -> None:
        now = utcnow()
        due = await self._store_call(
            lambda: list(
                self.store.db.keywords.find(
                    {"enabled": True, "next_run_at": {"$ne": None, "$lte": now}}
                )
            )
        )
        for keyword in due:
            await self._store_call(
                self.store.update_keyword,
                keyword["_id"],
                {
                    "next_run_at": next_scheduled_time(
                        keyword["next_run_at"], keyword["interval_minutes"], now
                    )
                },
            )
        for keyword in due:
            await self.start_batch(keyword["_id"], trigger="schedule")

    async def cancel_batch(self, batch_id: str) -> bool:
        task = self._tasks.get(batch_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True
        # Recover a database-visible run left behind by a restart or a task crash.
        batch = await self._store_call(self.store.batch, batch_id)
        if not batch or batch["state"] not in {"queued", "running"}:
            return False
        now = utcnow()
        for source_run in batch.get("source_runs", []):
            if source_run["state"] in {"queued", "running"}:
                await self._store_call(
                    self.store.update_source_run,
                    source_run["id"],
                    {
                        "state": "cancelled",
                        "phase": "cancelled",
                        "message": "Cancelled after recovering a stale run",
                        "finished_at": now,
                        "heartbeat_at": now,
                    },
                )
        await self._store_call(
            self.store.update_batch,
            batch_id,
            {
                "state": "cancelled",
                "error_message": "Cancelled after recovering a stale run",
                "finished_at": now,
            },
        )
        await self.events.publish(
            {"type": "batch", "batch_id": batch_id, "state": "cancelled", "recovered": True}
        )
        return True

    def cleanup_retention(self, days: int = 90) -> int:
        cutoff = utcnow() - timedelta(days=days)
        ids = [row["_id"] for row in self.store.db.content_items.find({"last_seen_at": {"$lt": cutoff}}, {"_id": 1})]
        for content_item_id in ids:
            self.store.delete_item(content_item_id)
        return len(ids)

    def cleanup_interrupted(self) -> int:
        detail = "API restarted before this run completed. Start a new manual run when ready."
        batches = list(self.store.db.crawl_batches.find({"state": {"$in": ["queued", "running"]}}))
        now = utcnow()
        for batch in batches:
            runs = self.store.source_runs(batch["_id"])
            for source_run in runs:
                if source_run["state"] in {"queued", "running"}:
                    self.store.update_source_run(
                        source_run["id"],
                        {
                            "state": "failed",
                            "phase": "failed",
                            "message": detail,
                            "error_message": detail,
                            "finished_at": now,
                            "heartbeat_at": now,
                        },
                    )
                    source_run["state"] = "failed"
            state = "partial" if any(row["state"] in {"succeeded", "skipped"} for row in runs) else "failed"
            self.store.update_batch(
                batch["_id"],
                {"state": state, "error_message": detail, "finished_at": now},
            )
        return len(batches)

    def cleanup_irrelevant(self) -> tuple[int, int]:
        matches = list(self.store.db.item_keyword_matches.find({"relevance_score": {"$lte": 0}}))
        candidates = {row["content_item_id"] for row in matches}
        if matches:
            self.store.db.item_keyword_matches.delete_many({"_id": {"$in": [row["_id"] for row in matches]}})
        removed_items = 0
        for content_item_id in candidates:
            if self.store.db.item_keyword_matches.find_one({"content_item_id": content_item_id}) is None:
                self.store.delete_item(content_item_id)
                removed_items += 1
        return len(matches), removed_items

    def rescore_keyword(self, keyword_id: int) -> tuple[int, int]:
        keyword = self.store.keyword(keyword_id)
        if not keyword:
            return 0, 0
        include_terms = clean_terms([keyword["name"], *keyword.get("include_terms", [])])
        exclude_terms = clean_terms(keyword.get("exclude_terms", []))
        rows = self.store.item_matches(keyword_id)
        removed = 0
        for item, match in rows:
            score, reasons = relevance(
                item.get("title", ""),
                item.get("body_snippet", ""),
                item.get("hashtags", []),
                item.get("author", ""),
                include_terms,
                exclude_terms,
            )
            if score <= 0:
                self.store.delete_match(item["id"], keyword_id)
                removed += 1
            else:
                self.store.save_match(
                    item["id"],
                    keyword_id,
                    {
                        "relevance_score": score,
                        "trend_score": self._trend_score(item, score),
                        "match_reasons": reasons,
                        "updated_at": utcnow(),
                    },
                )
        return len(rows), removed
