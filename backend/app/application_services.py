"""Application-owned resources, created at startup and stopped in dependency order."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException, Request

from .config import settings
from .crawlers import SOURCE_REGISTRY
from .crawlers.adapters.tiktok import TikTokOAuthConfig, TikTokTokenVault
from .mongo import create_store
from .services.channel_scans import CHANNEL_SCANNERS
from .services.connectors import SourceConnector, default_connectors
from .services.crawler_login import CrawlerLoginManager
from .services.credential_resolver import credential, gemini_credentials
from .services.gemini_subtitles import GeminiSubtitleService, GeminiSubtitleSettings
from .services.http_pool import close_http_pools
from .services.live_wall import LiveWallManager
from .services.runs import EventBus, RunManager, utcnow
from .services.subtitle_jobs import SubtitleJobManager
from .services.tiktok_oauth import TikTokOAuthService
from .services.video_thumbnails import VideoThumbnails
from .services.video_downloads import VideoDownloadManager
from .services.voiceover.manager import VoiceManager
from .services.voiceover.store import VoiceStore
from .storage_protocol import PersistenceStore

logger = logging.getLogger(__name__)


@dataclass
class AppServices:
    store: PersistenceStore
    connectors: dict[str, SourceConnector]
    events: EventBus
    run_manager: RunManager
    crawler_login_manager: CrawlerLoginManager
    live_wall_manager: LiveWallManager
    tiktok_oauth_service: TikTokOAuthService
    subtitle_jobs: SubtitleJobManager
    gemini_subtitle_jobs: SubtitleJobManager
    gemini_subtitle_service: GeminiSubtitleService
    voiceover_manager: VoiceManager
    video_thumbnails: VideoThumbnails
    video_downloads: VideoDownloadManager
    scheduler_task: asyncio.Task | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    @classmethod
    def create(cls, *, storage: PersistenceStore | None = None) -> AppServices:
        storage = storage if storage is not None else create_store()
        connectors = default_connectors()
        SOURCE_REGISTRY.validate_bindings(
            connectors, CHANNEL_SCANNERS, renderer_ids={"x", "tiktok"}
        )
        events = EventBus()
        return cls(
            store=storage,
            connectors=connectors,
            events=events,
            run_manager=RunManager(connectors, events, storage),
            crawler_login_manager=CrawlerLoginManager(),
            live_wall_manager=LiveWallManager(
                profile_root=settings.data_dir / "live-wall-profiles",
                browser_executable=settings.content_bot_coccoc_executable_path,
            ),
            tiktok_oauth_service=TikTokOAuthService(
                TikTokTokenVault(settings.data_dir / "crawler-secrets" / "tiktok"),
                lambda: TikTokOAuthConfig(
                    credential("tiktok_client_key", ""),
                    credential("tiktok_client_secret", "") or "",
                    credential("tiktok_redirect_uri", ""),
                ),
                settings.content_bot_frontend_url,
            ),
            subtitle_jobs=SubtitleJobManager(
                settings.data_dir / "subtitle-jobs",
                max_workers=settings.content_bot_subtitle_job_concurrency,
            ),
            gemini_subtitle_jobs=SubtitleJobManager(
                settings.data_dir / "gemini-subtitle-jobs", max_workers=1,
                recoverable_kinds=("generation",),
            ),
            gemini_subtitle_service=GeminiSubtitleService(
                GeminiSubtitleSettings(
                    job_root=Path(tempfile.gettempdir()) / "content-bot-gemini-jobs",
                    model=settings.content_bot_gemini_model,
                    timeout_seconds=settings.content_bot_gemini_timeout_seconds,
                    chunk_seconds=settings.content_bot_gemini_chunk_seconds,
                    max_input_mb=settings.content_bot_gemini_max_input_mb,
                    max_retries=settings.content_bot_gemini_max_retries,
                    retry_base_seconds=settings.content_bot_gemini_retry_base_seconds,
                    max_concurrent=settings.content_bot_gemini_max_concurrent,
                    group_concurrent=settings.content_bot_gemini_group_concurrent,
                    compression_concurrent=settings.content_bot_gemini_compression_concurrent,
                    max_chunk_seconds=settings.content_bot_gemini_max_chunk_seconds,
                    min_pause_ms=settings.content_bot_gemini_min_pause_ms,
                    context_seconds=settings.content_bot_gemini_context_seconds,
                    checkpoint_retention_days=settings.content_bot_gemini_checkpoint_retention_days,
                    checkpoint_max_mb=settings.content_bot_gemini_checkpoint_max_mb,
                ),
                api_key_provider=lambda: credential("gemini_api_key"),
                keyring_provider=gemini_credentials,
            ),
            voiceover_manager=VoiceManager(VoiceStore(settings.data_dir / "voiceover")),
            video_thumbnails=VideoThumbnails(
                timeout_seconds=settings.content_bot_thumbnail_timeout_seconds
            ),
            video_downloads=VideoDownloadManager(settings.data_dir / "videos"),
        )

    async def start(self, *, scheduler: bool = True) -> None:
        self.run_manager.store = self.store
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.video_downloads.start)
        await asyncio.to_thread(self.store.initialize)
        # Import after application construction: the API and startup share the
        # same persisted generation recipe and checkpoint validation.
        from .api.subtitles import recover_gemini_generation_jobs
        await asyncio.to_thread(recover_gemini_generation_jobs, self)
        if not self.store.is_available:
            logger.warning(
                "%s storage is unavailable; scheduler is disabled",
                self.store.storage_name,
            )
            return
        await asyncio.to_thread(self.run_manager.cleanup_interrupted)
        await asyncio.to_thread(self._cleanup_migration)
        if scheduler:
            self.scheduler_task = asyncio.create_task(
                self._scheduler_loop(), name="content-bot-scheduler"
            )

    def _cleanup_migration(self) -> None:
        if self.store.metadata("cleanup-irrelevant-v1") is None:
            self.run_manager.cleanup_irrelevant()
            self.store.set_metadata(
                "cleanup-irrelevant-v1", {"completed": True, "completed_at": utcnow()}
            )

    async def _scheduler_loop(self) -> None:
        interval = timedelta(
            hours=max(1, settings.content_bot_crawler_retention_interval_hours)
        )
        next_retention_at = utcnow()
        while True:
            now = utcnow()
            if now >= next_retention_at:
                await self.run_manager._store_call(
                    self.run_manager.cleanup_retention,
                    max(1, settings.content_bot_crawler_retention_days),
                )
                next_retention_at = now + interval
            await self.run_manager.scheduler_tick()
            await asyncio.sleep(30)

    async def shutdown(self, *, timeout_seconds: float = 10) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[Exception] = []
        for manager in (
            self.run_manager,
            self.subtitle_jobs,
            self.gemini_subtitle_jobs,
            self.voiceover_manager,
            self.video_downloads,
        ):
            try:
                manager.stop_accepting()
            except Exception as exc:
                failures.append(exc)

        async def close(operation):
            try:
                await operation
            except Exception as exc:
                failures.append(exc)

        if self.scheduler_task is not None:
            self.scheduler_task.cancel()
            try:
                await asyncio.wait_for(self.scheduler_task, timeout_seconds)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                failures.append(exc)
        await close(self.run_manager.shutdown(timeout_seconds))
        await asyncio.gather(
            close(
                asyncio.to_thread(
                    self.subtitle_jobs.shutdown, timeout_seconds=timeout_seconds
                )
            ),
            close(
                asyncio.to_thread(
                    self.gemini_subtitle_jobs.shutdown, timeout_seconds=timeout_seconds
                )
            ),
            close(
                asyncio.to_thread(
                    self.voiceover_manager.shutdown, timeout_seconds=timeout_seconds
                )
            ),
            close(asyncio.to_thread(self.video_thumbnails.shutdown)),
            close(asyncio.to_thread(self.video_downloads.shutdown, timeout_seconds=timeout_seconds)),
            close(asyncio.wait_for(self.live_wall_manager.shutdown(), timeout_seconds)),
            close(
                asyncio.wait_for(self.crawler_login_manager.shutdown(), timeout_seconds)
            ),
        )
        await close(close_http_pools())
        client = getattr(self.store, "client", None)
        if client is not None:
            await close(asyncio.to_thread(client.close))
        if failures:
            raise ExceptionGroup(
                "Application shutdown did not complete cleanly", failures
            )


def get_services(request: Request) -> AppServices:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(503, "Ứng dụng chưa sẵn sàng. Vui lòng thử lại sau.")
    return services
