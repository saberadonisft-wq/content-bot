from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..crawlers.runtime import (
    CancellationToken,
    RunBudgets,
)
from .connector_contracts import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from .connector_support import (
    logger,
    login_progress_from_stderr,
)


class JsonlCommandConnector(SourceConnector):
    """Runs an explicit local adapter; stdout must contain newline-delimited JSON items.

    This keeps MediaCrawler/browser automation out of the API process and never
    tries to create a login session or evade a platform challenge automatically.
    """

    def __init__(
        self,
        source_id: str,
        label: str,
        detail: str,
        capabilities: ConnectorCapabilities,
        platform: str,
    ):
        self.source_id = source_id
        self.label = label
        self.group = "MediaCrawler bridge"
        self.detail = detail
        self.capabilities = capabilities
        self.platform = platform

    async def healthcheck(self) -> ConnectorStatus:
        if settings.mediacrawler_command:
            return ConnectorStatus(
                "ready",
                "Custom local JSONL bridge configured. Browser login remains user-visible.",
            )
        project_root = Path(__file__).resolve().parents[3]
        adapter = project_root / "backend" / "scripts" / "mediacrawler_adapter.py"
        runtime = (
            project_root
            / "vendor"
            / "mediacrawler"
            / ".venv"
            / "Scripts"
            / "python.exe"
        )
        ready_marker = project_root / "data" / "mediacrawler-ready"
        if (
            not adapter.exists()
            or not (project_root / "vendor" / "mediacrawler" / "main.py").exists()
        ):
            return ConnectorStatus(
                "not_configured",
                "MediaCrawler submodule is missing. Run git submodule update --init --recursive.",
            )
        if not runtime.exists() or not ready_marker.exists():
            return ConnectorStatus(
                "setup_required",
                "Run .\\scripts\\setup-mediacrawler.ps1 once, then restart Content Bot.",
            )
        coccoc_path = Path(settings.content_bot_coccoc_executable_path).expanduser()
        if not coccoc_path.is_file():
            return ConnectorStatus(
                "setup_required",
                f"Cốc Cốc was not found at {coccoc_path}. Set CONTENT_BOT_COCCOC_EXECUTABLE_PATH in backend/.env.",
            )
        return ConnectorStatus(
            "ready",
            "Direct MediaCrawler adapter is installed. A visible browser opens when login is required.",
        )

    async def search(
        self, query: SearchQuery, checkpoint: dict[str, Any] | None = None
    ) -> AsyncIterator[RawContentItem]:
        """Run the bridge, recovering once from a browser target that closed early."""
        yielded_any = False
        for attempt in range(2):
            try:
                async for item in self._search_once(query):
                    yielded_any = True
                    yield item
                return
            except RuntimeError as exc:
                error_text = str(exc)
                target_closed = (
                    "TargetClosedError" in error_text
                    or "target page, context or browser has been closed"
                    in error_text.casefold()
                )
                if attempt or yielded_any or not target_closed:
                    raise
                if query.progress_callback:
                    try:
                        await query.progress_callback(
                            "retrying",
                            "Cốc Cốc page was closed; reopening the browser session",
                        )
                    except Exception:
                        logger.debug(
                            "Progress callback failed while retrying browser session",
                            exc_info=True,
                        )
                await asyncio.sleep(1)

    async def fetch_detail(self, target_url: str) -> RawContentItem:
        connector = self._tieba_target_connector()
        return await connector.fetch_detail(target_url)

    async def list_creator(
        self,
        target_url: str,
        *,
        max_items: int = 20,
        initial_cursor: str | None = None,
    ) -> AsyncIterator[RawContentItem]:
        connector = self._tieba_target_connector()
        async for item in connector.list_creator(
            target_url,
            max_items=max_items,
            initial_cursor=initial_cursor,
        ):
            yield item

    async def scan_channel(
        self,
        channel: dict[str, Any],
        query: SearchQuery,
    ) -> AsyncIterator[RawContentItem]:
        connector = self._tieba_target_connector()
        async for item in connector.scan_channel(channel, query):
            yield item

    async def list_comments(
        self,
        target_url: str,
        *,
        max_items: int = 20,
        initial_cursor: str | None = None,
    ):
        connector = self._tieba_target_connector()
        async for item in connector.list_comments(
            target_url,
            max_items=max_items,
            initial_cursor=initial_cursor,
        ):
            yield item

    async def scan_comments(
        self,
        target_url: str,
        budgets: RunBudgets,
        *,
        sort: str = "new",
        cancellation: CancellationToken | None = None,
    ):
        connector = self._tieba_target_connector()
        return await connector.scan_comments(
            target_url,
            budgets,
            sort=sort,
            cancellation=cancellation,
        )

    def _tieba_target_connector(self):
        if self.source_id != "tieba":
            raise RuntimeError("Clean-room target operations are only wired for Tieba")
        from .cbce_connectors import CbceTiebaConnector

        return CbceTiebaConnector()

    async def _search_once(self, query: SearchQuery) -> AsyncIterator[RawContentItem]:
        project_root = Path(__file__).resolve().parents[3]
        substitutions = {
            "source": self.platform,
            "keywords": ",".join(query.include_terms),
            "max_items": str(query.max_items),
            "profile_dir": str(settings.mediacrawler_profile_dir),
        }
        if settings.mediacrawler_command:
            command = [
                part.format(**substitutions)
                for part in shlex.split(settings.mediacrawler_command, posix=False)
            ]
        else:
            command = [
                sys.executable,
                str(project_root / "backend" / "scripts" / "mediacrawler_adapter.py"),
            ]
        command.extend(
            [
                "--source",
                self.platform,
                "--keywords",
                ",".join(query.include_terms),
                "--max-items",
                str(query.max_items),
                "--profile-dir",
                str(settings.mediacrawler_profile_dir),
            ]
        )
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_tail = bytearray()
        stderr_pending = ""

        async def report_progress_lines(lines: list[str]) -> None:
            if not query.progress_callback:
                return
            for line in lines:
                progress = login_progress_from_stderr(line)
                if not progress:
                    continue
                try:
                    await query.progress_callback(*progress)
                except Exception:
                    # Progress reporting must never stop the crawler process.
                    logger.debug("Progress callback failed", exc_info=True)
                    continue

        async def drain_stderr() -> None:
            nonlocal stderr_pending
            while chunk := await process.stderr.read(4096):
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 32_000:
                    del stderr_tail[:-32_000]
                stderr_pending += chunk.decode("utf-8", errors="replace")
                complete_lines = stderr_pending.split("\n")
                stderr_pending = complete_lines.pop()
                await report_progress_lines(complete_lines)
            if stderr_pending:
                await report_progress_lines([stderr_pending])

        stderr_task = asyncio.create_task(drain_stderr())
        yielded = 0
        try:
            async with asyncio.timeout(
                query.deadline_limit(settings.mediacrawler_timeout_seconds)
            ):
                async for line in process.stdout:
                    if yielded >= query.max_items:
                        await self._stop_process_tree(process)
                        break
                    try:
                        payload = json.loads(line.decode("utf-8"))
                        published = payload.get("published_at")
                        yield RawContentItem(
                            external_id=str(payload["external_id"]),
                            canonical_url=str(payload["canonical_url"]),
                            title=str(payload.get("title", "")),
                            body_snippet=str(payload.get("body_snippet", ""))[:4000],
                            author=str(payload.get("author", "")),
                            hashtags=[str(tag) for tag in payload.get("hashtags", [])],
                            locale=payload.get("locale"),
                            published_at=datetime.fromisoformat(published)
                            if published
                            else None,
                            metrics={
                                key: int(value or 0)
                                for key, value in payload.get("metrics", {}).items()
                            },
                            raw_payload=payload,
                        )
                        yielded += 1
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                await process.wait()
        except TimeoutError as exc:
            raise RuntimeError(
                "MediaCrawler timed out while waiting for login or source results."
            ) from exc
        finally:
            if process.returncode is None:
                await self._stop_process_tree(process)
            await stderr_task
        if process.returncode not in (0, None):
            detail = stderr_tail.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(detail or f"Bridge exited {process.returncode}")

    @staticmethod
    async def _stop_process_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
