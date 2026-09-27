"""Video acquisition runs and their bridge to the durable video downloader.

The acquisition layer owns discovery metadata and selection intent.  It does
not download media itself; :class:`VideoDownloadManager` remains the owner of
download processes and published files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from ..config import settings
from ..crawlers.runtime import normalize_account_ref
from ..storage_protocol import PersistenceStore
from .subtitle_jobs import SubtitleJobQueueFull
from .video_downloads import VideoDownloadManager, normalize_video_url

logger = logging.getLogger(__name__)

RUNS = "acquisition_runs"
CANDIDATES = "acquisition_candidates"
RUN_ITEMS = "acquisition_run_items"
INTENTS = "acquisition_download_intents"
CHANNELS = "acquisition_channels"
SELECTIONS = "acquisition_selections"
SUBSCRIPTION_OBSERVATIONS = "acquisition_subscription_observations"
_UNSET = object()

ACTIVE_RUN_STATES = {"queued", "running", "paused", "waiting_auth"}
MODES = {"video", "creator", "playlist", "search"}
_METADATA_RETRY_CODES = frozenset({"RATE_LIMITED"})
_METADATA_MAX_ATTEMPTS = 3

_HOST_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("youtube", ("youtube.com", "youtu.be")),
    ("bilibili", ("bilibili.com", "b23.tv", "bilibili.tv")),
    ("douyin", ("douyin.com",)),
    ("tiktok", ("tiktok.com",)),
    ("xhs", ("xiaohongshu.com", "rednote.com", "xhslink.com")),
    ("kuaishou", ("kuaishou.com", "快手.com")),
    ("weibo", ("weibo.com", "weibo.cn")),
    ("facebook", ("facebook.com", "fb.watch")),
    ("instagram", ("instagram.com",)),
    ("x", ("x.com", "twitter.com")),
    ("vimeo", ("vimeo.com",)),
)


class AcquisitionError(ValueError):
    """A safe, user-facing acquisition validation or provider error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_REQUEST",
        retry_after: int | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.run_id = run_id
        self.job_id = job_id


class AcquisitionManager:
    """Durable metadata discovery with bounded background extraction."""

    def __init__(
        self,
        store: PersistenceStore,
        video_downloads: VideoDownloadManager,
        *,
        max_workers: int = 2,
        max_pending: int = 32,
        extractor: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
        connectors: Mapping[str, Any] | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.store = store
        self._max_pending = max_pending
        self.video_downloads = video_downloads
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="video-acquisition"
        )
        self._extractor = extractor
        self._connectors = dict(connectors or {})
        self._bilibili_connector = (connectors or {}).get("bilibili")
        self._tiktok_connector = (connectors or {}).get("tiktok")
        self._lock = threading.RLock()
        self._scheduler_lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._accepting = True

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _ensure_feature_enabled() -> None:
        if not settings.content_bot_acquisition_enabled:
            raise AcquisitionError(
                "Tính năng cào video đang tắt trong rollout.",
                code="FEATURE_DISABLED",
            )

    @staticmethod
    def _public(document: dict[str, Any] | None) -> dict[str, Any] | None:
        return deepcopy(document) if document is not None else None

    @staticmethod
    def _source_for_url(value: str) -> str | None:
        try:
            host = (urlsplit(value).hostname or "").casefold().rstrip(".")
        except ValueError:
            return None
        for source_id, roots in _HOST_SOURCES:
            if any(host == root or host.endswith("." + root) for root in roots):
                return source_id
        return None

    @classmethod
    def _canonical_url(cls, value: str, fallback: str | None = None) -> str:
        for candidate in (value, fallback):
            if not candidate:
                continue
            try:
                normalized = normalize_video_url(candidate)
            except ValueError:
                continue
            if cls._source_for_url(normalized):
                return normalized
        raise AcquisitionError(
            "Không tìm thấy link video gốc an toàn trong kết quả nguồn.",
            code="CONTENT_UNAVAILABLE",
        )

    @staticmethod
    def _published_at(value: Any) -> str | None:
        if isinstance(value, str) and len(value) == 8 and value.isdigit():
            try:
                return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC).isoformat()
            except ValueError:
                return None
        if isinstance(value, (int, float)) and value > 0:
            try:
                return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        return value if isinstance(value, str) else None

    @classmethod
    def _matches_filters(
        cls, candidate: dict[str, Any], filters: dict[str, Any]
    ) -> bool:
        media_type = str(filters.get("media_type") or "").strip().casefold()
        if media_type and str(candidate.get("media_type") or "").casefold() != media_type:
            return False
        title_contains = str(filters.get("title_contains") or "").strip().casefold()
        if title_contains and title_contains not in str(candidate.get("title") or "").casefold():
            return False
        creator_id = str(filters.get("creator_id") or "").strip()
        if creator_id and str(candidate.get("creator_id") or "") != creator_id:
            return False
        duration = candidate.get("duration_seconds")
        if (
            filters.get("min_duration_seconds") is not None
            and (
                not isinstance(duration, (int, float))
                or duration < float(filters["min_duration_seconds"])
            )
        ):
            return False
        if (
            filters.get("max_duration_seconds") is not None
            and (
                not isinstance(duration, (int, float))
                or duration > float(filters["max_duration_seconds"])
            )
        ):
            return False
        published_after = cls._parse_iso(filters.get("published_after"))
        if published_after is not None:
            candidate_published = cls._parse_iso(candidate.get("published_at"))
            if candidate_published is None or candidate_published < published_after:
                return False
        return True

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _safe_thumbnail(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) > 4096:
            return None
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))

    @staticmethod
    def _candidate_id(source_id: str, external_id: str, media_id: str) -> str:
        raw = f"{source_id}\x00{external_id}\x00{media_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def _normalize_entry(
        cls,
        entry: dict[str, Any],
        *,
        fallback_url: str,
        fallback_source: str,
        position: int,
    ) -> dict[str, Any]:
        source_id = cls._source_for_url(str(entry.get("webpage_url") or "")) or fallback_source
        external_id = str(entry.get("id") or entry.get("display_id") or "").strip()
        if not external_id:
            external_id = hashlib.sha256(
                str(entry.get("title") or fallback_url).encode("utf-8")
            ).hexdigest()[:24]
        media_id = external_id
        candidate_id = cls._candidate_id(source_id, external_id, media_id)

        raw_url = entry.get("webpage_url") or entry.get("original_url")
        canonical_url = cls._canonical_url(str(raw_url or ""), fallback_url)
        query = parse_qs(urlsplit(canonical_url).query, keep_blank_values=True)
        part_index: int | None = None
        if source_id == "bilibili" and query.get("p"):
            try:
                part_index = int(query["p"][0])
            except (TypeError, ValueError):
                part_index = None
        if part_index is not None:
            media_id = f"{external_id}:p{part_index}"
            candidate_id = cls._candidate_id(source_id, external_id, media_id)

        vcodec = str(entry.get("vcodec") or "")
        media_type = "audio" if vcodec == "none" else "video"
        live_status = str(entry.get("live_status") or "").casefold()
        if entry.get("is_live") or live_status in {"is_live", "is_upcoming"}:
            media_type = "live"
        candidate = {
            "_id": candidate_id,
            "source_id": source_id,
            "provider_id": str(entry.get("_provider_id") or "yt-dlp"),
            "external_id": external_id,
            "media_id": media_id,
            "part_index": part_index,
            "canonical_url": canonical_url,
            "title": str(entry.get("title") or "").strip()[:500],
            "uploader": str(entry.get("uploader") or entry.get("channel") or "").strip()[:240],
            "creator_id": str(entry.get("channel_id") or entry.get("uploader_id") or "").strip()[:240],
            "thumbnail_url": cls._safe_thumbnail(entry.get("thumbnail")),
            "published_at": cls._published_at(entry.get("timestamp") or entry.get("upload_date")),
            "duration_seconds": (
                float(entry["duration"])
                if isinstance(entry.get("duration"), (int, float)) and entry["duration"] >= 0
                else None
            ),
            "media_type": media_type,
            "availability": "live" if media_type == "live" else "metadata",
            "download_available": entry.get("_download_available") is not False,
            "metrics": {
                key: entry.get(key)
                for key in ("view_count", "like_count", "comment_count", "repost_count")
                if isinstance(entry.get(key), (int, float))
            },
            "position": position,
            "updated_at": cls._now(),
        }
        if entry.get("_download_available") is False:
            candidate["download_unavailable_reason"] = str(
                entry.get("_download_unavailable_reason")
                or "Provider chỉ trả metadata; chưa có media download contract."
            )[:300]
        return candidate

    @staticmethod
    def _yt_dlp_entries(info: dict[str, Any], *, max_candidates: int) -> list[dict[str, Any]]:
        entries = info.get("entries")
        if entries is None:
            return [info]
        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            result.append(entry)
            if len(result) >= max_candidates:
                break
        return result

    @classmethod
    def _bilibili_playlist_metadata(
        cls,
        target: str,
        *,
        max_candidates: int,
        deadline_seconds: int,
        started: float,
        listing_offset: int = 0,
    ) -> dict[str, dict[str, Any]]:
        """Read bounded public playlist metadata for flat Bilibili entries.

        yt-dlp intentionally returns URL-only entries for some Bilibili
        favorites lists. The public resource endpoint supplies title/uploader
        and publication metadata without requiring a cookie or signed media
        URL. Failure is soft: callers may still use metadata already returned
        by yt-dlp, but they must not manufacture a title or playlist URL.
        """
        try:
            from ..crawlers.adapters.bilibili.targets import (
                BilibiliTargetKind,
                parse_bilibili_target,
            )

            parsed_target = parse_bilibili_target(target)
            if parsed_target.kind is not BilibiliTargetKind.PLAYLIST:
                return {}
            media_id = str(parsed_target.external_id or "").removeprefix("ml")
            if not re.fullmatch(r"[1-9][0-9]*", media_id):
                return {}
        except (TypeError, ValueError):
            return {}

        result: dict[str, dict[str, Any]] = {}
        page_size = min(max(max_candidates, 1), 20)
        first_page_offset = listing_offset % page_size
        max_pages = min(
            20,
            (first_page_offset + max_candidates + page_size - 1) // page_size,
        )
        first_page = listing_offset // page_size + 1
        try:
            for page_number in range(first_page, first_page + max_pages):
                elapsed = time.monotonic() - started
                if elapsed >= deadline_seconds:
                    break
                query = urlencode({"media_id": media_id, "pn": page_number, "ps": page_size})
                endpoint = (
                    "https://api.bilibili.com/x/v3/fav/resource/list?"
                    f"{query}"
                )
                request = Request(
                    endpoint,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "ContentBot/1.0 acquisition metadata",
                    },
                )
                timeout = max(1.0, min(30.0, deadline_seconds - elapsed))
                with urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read(1_000_000).decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("code") != 0:
                    break
                data = payload.get("data")
                if not isinstance(data, dict):
                    break
                medias = data.get("medias")
                if not isinstance(medias, list):
                    break
                for item in medias:
                    if not isinstance(item, dict):
                        continue
                    bvid = str(item.get("bvid") or item.get("bv_id") or "").strip()
                    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                        continue
                    upper = item.get("upper") if isinstance(item.get("upper"), dict) else {}
                    counts = item.get("cnt_info") if isinstance(item.get("cnt_info"), dict) else {}
                    result[bvid] = {
                        "webpage_url": f"https://www.bilibili.com/video/{bvid}",
                        "title": str(item.get("title") or "").strip(),
                        "uploader": str(upper.get("name") or "").strip(),
                        "uploader_id": str(upper.get("mid") or "").strip(),
                        "thumbnail": item.get("cover"),
                        "timestamp": item.get("pubtime") or item.get("ctime"),
                        "duration": item.get("duration"),
                        "view_count": counts.get("play"),
                        "like_count": counts.get("like"),
                        "comment_count": counts.get("reply"),
                    }
                if not data.get("has_more") or len(result) >= max_candidates:
                    break
        except Exception:
            logger.warning("Bilibili playlist metadata enrichment failed", exc_info=True)
        return result

    def _uses_bilibili_creator_provider(self, request: dict[str, Any]) -> bool:
        requested_provider = str(request.get("provider_id") or "").strip()
        if requested_provider not in {"", "cbce_bilibili"} or request.get("mode") != "creator":
            return False
        connector = self._bilibili_connector
        if connector is None or not callable(getattr(connector, "list_creator", None)):
            return False
        if not bool(getattr(connector, "configured", False)):
            return False
        targets = request.get("targets") or []
        return (
            len(targets) == 1
            and self._source_for_url(str(targets[0])) == "bilibili"
        )

    def _uses_bilibili_search_provider(self, request: dict[str, Any]) -> bool:
        requested_provider = str(request.get("provider_id") or "").strip()
        if (
            requested_provider not in {"", "cbce_bilibili"}
            or request.get("mode") != "search"
            or str(request.get("source_id") or "").casefold() != "bilibili"
        ):
            return False
        connector = self._bilibili_connector
        provider = getattr(getattr(connector, "spec", None), "provider_id", None)
        return bool(
            connector is not None
            and provider == "cbce_bilibili"
            and callable(getattr(connector, "search", None))
            and getattr(connector, "configured", False)
        )

    def _uses_bilibili_detail_provider(self, request: dict[str, Any]) -> bool:
        """Use CBCE detail when a caller selected a Bilibili profile."""
        if request.get("mode") != "video" or not request.get("connection_id"):
            return False
        requested_provider = str(request.get("provider_id") or "").strip()
        if requested_provider not in {"", "cbce_bilibili"}:
            return False
        if self._bilibili_connector is None or not callable(
            getattr(self._bilibili_connector, "for_account", None)
        ):
            return False
        connector = self._bilibili_connector_for_request(request)
        return bool(
            connector is not None
            and callable(getattr(connector, "fetch_detail", None))
            and getattr(connector, "configured", False)
        )

    def _uses_tiktok_creator_provider(self, request: dict[str, Any]) -> bool:
        requested_provider = str(request.get("provider_id") or "").strip()
        if requested_provider not in {"", "tiktok_display"} or request.get("mode") != "creator":
            return False
        targets = request.get("targets") or []
        connector = self._tiktok_connector
        return bool(
            len(targets) == 1
            and self._source_for_url(str(targets[0])) == "tiktok"
            and connector is not None
            and callable(getattr(connector, "scan_channel", None))
            and getattr(connector, "configured", False)
        )

    def _uses_tiktok_detail_provider(self, request: dict[str, Any]) -> bool:
        requested_provider = str(request.get("provider_id") or "").strip()
        if requested_provider not in {"", "tiktok_display"} or request.get("mode") != "video":
            return False
        targets = request.get("targets") or []
        connector = self._tiktok_connector
        return bool(
            targets
            and all(self._source_for_url(str(target)) == "tiktok" for target in targets)
            and connector is not None
            and callable(getattr(connector, "fetch_detail", None))
            and getattr(connector, "configured", False)
        )

    def _uses_douyin_search_provider(self, request: dict[str, Any]) -> bool:
        requested_provider = str(request.get("provider_id") or "").strip()
        if (
            requested_provider not in {"", "cbce_douyin"}
            or request.get("mode") != "search"
            or request.get("source_id") != "douyin"
        ):
            return False
        connector = self._connectors.get("douyin")
        spec = getattr(connector, "spec", None)
        provider_id = str(
            getattr(connector, "provider_id", None)
            or getattr(spec, "provider_id", "")
        ).strip()
        return bool(
            connector is not None
            and provider_id == "cbce_douyin"
            and callable(getattr(connector, "search", None))
            and getattr(connector, "configured", False)
        )

    @staticmethod
    def _map_crawler_failure(error: Exception) -> AcquisitionError:
        code = str(getattr(getattr(error, "code", None), "value", ""))
        mapped = {
            "AUTH_REQUIRED": "AUTH_REQUIRED",
            "AUTH_TIMEOUT": "AUTH_REQUIRED",
            "CHALLENGE_REQUIRED": "AUTH_REQUIRED",
            "PERMISSION_REQUIRED": "AUTH_REQUIRED",
            "RATE_LIMITED": "RATE_LIMITED",
            "NOT_FOUND": "CONTENT_UNAVAILABLE",
            "UNSUPPORTED": "UNSUPPORTED_OPERATION",
        }.get(code, "SOURCE_UNAVAILABLE")
        message = str(getattr(error, "safe_message", None) or error)
        retry_after = getattr(error, "retry_after_seconds", None)
        if retry_after is None and mapped == "RATE_LIMITED":
            # Browser providers often cannot expose Retry-After. Keep the
            # retry contract useful without pretending the provider supplied an
            # exact reset time.
            retry_after = 60
        else:
            try:
                retry_after = (
                    min(7 * 24 * 60 * 60, max(0, math.ceil(float(retry_after))))
                    if retry_after is not None
                    else None
                )
            except (TypeError, ValueError, OverflowError):
                retry_after = None
        return AcquisitionError(message[:500], code=mapped, retry_after=retry_after)

    def _bilibili_connector_for_request(
        self, request: dict[str, Any]
    ) -> Any | None:
        connector = self._bilibili_connector
        connection_id = request.get("connection_id")
        if not connection_id:
            return connector
        if connector is None:
            raise AcquisitionError(
                "Bilibili session connection chưa được cấu hình.",
                code="UNSUPPORTED_OPERATION",
            )
        factory = getattr(connector, "for_account", None)
        if not callable(factory):
            raise AcquisitionError(
                "Provider Bilibili chưa hỗ trợ connection profile riêng.",
                code="UNSUPPORTED_OPERATION",
            )
        try:
            return factory(connection_id)
        except (TypeError, ValueError) as exc:
            raise AcquisitionError(
                "Connection profile Bilibili không hợp lệ.",
                code="INVALID_REQUEST",
            ) from exc

    async def _extract_bilibili_creator(
        self, request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        connector = self._bilibili_connector_for_request(request)
        if connector is None:
            raise AcquisitionError(
                "Provider Bilibili creator chưa được cấu hình.",
                code="UNSUPPORTED_OPERATION",
            )
        limits = request.get("limits") or {}
        max_items = min(max(int(limits.get("max_candidates", 20)), 1), 100)
        target = str((request.get("targets") or [""])[0])
        results: list[dict[str, Any]] = []
        async for item in connector.list_creator(target, max_items=max_items):
            published = getattr(item, "published_at", None)
            if published is not None and published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            timestamp = published.timestamp() if published is not None else None
            metrics = getattr(item, "metrics", {}) or {}
            raw_entry = {
                "id": getattr(item, "external_id", ""),
                "webpage_url": getattr(item, "canonical_url", target),
                "title": getattr(item, "title", ""),
                "uploader": getattr(item, "author", ""),
                "timestamp": timestamp,
                "view_count": metrics.get("view_count"),
                "like_count": metrics.get("like_count"),
                "comment_count": metrics.get("comment_count"),
                "_provider_id": "cbce_bilibili",
            }
            results.append(
                self._normalize_entry(
                    raw_entry,
                    fallback_url=target,
                    fallback_source="bilibili",
                    position=len(results) + 1,
                )
            )
            if len(results) >= max_items:
                break
        return results

    @staticmethod
    def _connector_item_entry(item: Any, *, provider_id: str) -> dict[str, Any]:
        published = getattr(item, "published_at", None)
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        timestamp = published.timestamp() if published is not None else None
        metrics = getattr(item, "metrics", {}) or {}
        thumbnail: str | None = None
        for media_item in getattr(item, "media", ()) or ():
            if not isinstance(media_item, Mapping):
                continue
            if str(media_item.get("kind") or "").strip() != "cover":
                continue
            candidate_thumbnail = media_item.get("url")
            if isinstance(candidate_thumbnail, str) and candidate_thumbnail.strip():
                thumbnail = candidate_thumbnail.strip()
                break
        entry = {
            "id": getattr(item, "external_id", ""),
            "webpage_url": getattr(item, "canonical_url", ""),
            "title": getattr(item, "title", ""),
            "uploader": getattr(item, "author", ""),
            "timestamp": timestamp,
            "thumbnail": thumbnail,
            "view_count": metrics.get("view_count"),
            "like_count": metrics.get("like_count"),
            "comment_count": metrics.get("comment_count"),
            "_provider_id": provider_id,
        }
        if provider_id in {"tiktok_display", "cbce_douyin"}:
            entry["_download_available"] = False
            entry["_download_unavailable_reason"] = (
                "TikTok Display API hiện chỉ trả metadata; chưa có media download contract."
                if provider_id == "tiktok_display"
                else "CBCE Douyin search hiện chỉ trả metadata; chưa có media download contract."
            )
        return entry

    async def _extract_bilibili_search(
        self, request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        connector = self._bilibili_connector_for_request(request)
        if connector is None:
            raise AcquisitionError(
                "Provider Bilibili search chưa được cấu hình.",
                code="UNSUPPORTED_OPERATION",
            )
        from .connector_contracts import SearchQuery

        limits = request.get("limits") or {}
        max_items = min(max(int(limits.get("max_candidates", 20)), 1), 100)
        query = SearchQuery(
            keyword_id=0,
            name=str(request.get("query") or ""),
            include_terms=[],
            max_items=max_items,
            deadline_seconds=min(max(int(limits.get("deadline_seconds", 300)), 30), 600),
        )
        results: list[dict[str, Any]] = []
        async for item in connector.search(query):
            results.append(
                self._normalize_entry(
                    self._connector_item_entry(item, provider_id="cbce_bilibili"),
                    fallback_url=str(getattr(item, "canonical_url", "")),
                    fallback_source="bilibili",
                    position=len(results) + 1,
                )
            )
            if len(results) >= max_items:
                break
        return results

    async def _extract_bilibili_detail(
        self, request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        connector = self._bilibili_connector_for_request(request)
        if connector is None:
            raise AcquisitionError(
                "Provider Bilibili detail chưa được cấu hình.",
                code="UNSUPPORTED_OPERATION",
            )
        results: list[dict[str, Any]] = []
        maximum = int((request.get("limits") or {}).get("max_candidates", 20))
        for target in (request.get("targets") or [])[:maximum]:
            item = await connector.fetch_detail(target)
            entry = self._connector_item_entry(item, provider_id="cbce_bilibili")
            # Keep the caller's part selector in the acquisition key.
            entry["webpage_url"] = target
            results.append(
                self._normalize_entry(
                    entry,
                    fallback_url=target,
                    fallback_source="bilibili",
                    position=len(results) + 1,
                )
            )
        return results

    async def _extract_tiktok_creator(
        self, request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        connector = self._tiktok_connector
        if connector is None:
            raise AcquisitionError(
                "Provider TikTok Display chưa được cấu hình.",
                code="UNSUPPORTED_OPERATION",
            )
        from .connector_contracts import SearchQuery

        limits = request.get("limits") or {}
        max_items = min(max(int(limits.get("max_candidates", 20)), 1), 100)
        target = str((request.get("targets") or [""])[0])
        query = SearchQuery(
            keyword_id=0,
            name=target,
            include_terms=[],
            max_items=max_items,
            deadline_seconds=min(
                max(int(limits.get("deadline_seconds", 300)), 30), 600
            ),
        )
        results: list[dict[str, Any]] = []
        async for item in connector.scan_channel(
            {"url": target, "normalized_url": target}, query
        ):
            results.append(
                self._normalize_entry(
                    self._connector_item_entry(item, provider_id="tiktok_display"),
                    fallback_url=str(getattr(item, "canonical_url", target)),
                    fallback_source="tiktok",
                    position=len(results) + 1,
                )
            )
            if len(results) >= max_items:
                break
        return results

    async def _extract_tiktok_detail(
        self, request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        connector = self._tiktok_connector
        if connector is None:
            raise AcquisitionError(
                "Provider TikTok Display chưa được cấu hình.",
                code="UNSUPPORTED_OPERATION",
            )
        results: list[dict[str, Any]] = []
        maximum = min(max(int((request.get("limits") or {}).get("max_candidates", 20)), 1), 100)
        for target in (request.get("targets") or [])[:maximum]:
            item = await connector.fetch_detail(target)
            results.append(
                self._normalize_entry(
                    self._connector_item_entry(item, provider_id="tiktok_display"),
                    fallback_url=str(getattr(item, "canonical_url", target)),
                    fallback_source="tiktok",
                    position=len(results) + 1,
                )
            )
        return results

    async def _extract_douyin_search(
        self, request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        connector = self._connectors.get("douyin")
        if connector is None:
            raise AcquisitionError(
                "Provider CBCE Douyin search chưa được cấu hình.",
                code="UNSUPPORTED_OPERATION",
            )
        from .connector_contracts import SearchQuery

        limits = request.get("limits") or {}
        max_items = min(max(int(limits.get("max_candidates", 20)), 1), 100)
        query = SearchQuery(
            keyword_id=0,
            name=str(request.get("query") or ""),
            include_terms=[],
            max_items=max_items,
            deadline_seconds=min(
                max(int(limits.get("deadline_seconds", 300)), 30), 600
            ),
        )
        results: list[dict[str, Any]] = []
        async for item in connector.search(query):
            results.append(
                self._normalize_entry(
                    self._connector_item_entry(item, provider_id="cbce_douyin"),
                    fallback_url=str(getattr(item, "canonical_url", "")),
                    fallback_source="douyin",
                    position=len(results) + 1,
                )
            )
            if len(results) >= max_items:
                break
        return results

    async def _await_provider_operation(
        self,
        operation: Awaitable[list[dict[str, Any]]],
        cancellation: threading.Event,
        deadline_seconds: float,
    ) -> list[dict[str, Any]]:
        """Give CBCE's child worker the same cancel/deadline contract as yt-dlp."""
        task = asyncio.ensure_future(operation)
        deadline = time.monotonic() + deadline_seconds
        try:
            while True:
                if cancellation.is_set():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return []
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise AcquisitionError(
                        "Lượt lấy metadata vượt thời gian cho phép.",
                        code="DEADLINE_EXCEEDED",
                    )
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task), min(0.1, remaining)
                    )
                except TimeoutError:
                    continue
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _await_bilibili_operation(
        self,
        operation: Awaitable[list[dict[str, Any]]],
        cancellation: threading.Event,
        deadline_seconds: float,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for callers/tests using the old private name."""
        return await self._await_provider_operation(
            operation, cancellation, deadline_seconds
        )

    def _extract_with_retry(
        self, request: dict[str, Any], cancellation: threading.Event
    ) -> list[dict[str, Any]]:
        """Retry only transient provider throttling within one run budget."""
        limits = dict(request.get("limits") or {})
        deadline_seconds = max(0.0, float(limits.get("deadline_seconds", 300)))
        deadline = time.monotonic() + deadline_seconds
        for attempt in range(1, _METADATA_MAX_ATTEMPTS + 1):
            if cancellation.is_set():
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcquisitionError(
                    "Lượt cào đã hết thời gian metadata.",
                    code="DEADLINE_EXCEEDED",
                )
            attempt_request = deepcopy(request)
            attempt_limits = dict(attempt_request.get("limits") or {})
            attempt_limits["deadline_seconds"] = remaining
            attempt_request["limits"] = attempt_limits
            try:
                return (
                    self._extractor(attempt_request)
                    if self._extractor
                    else self._extract_request(attempt_request, cancellation)
                )
            except AcquisitionError as error:
                if error.code not in _METADATA_RETRY_CODES or attempt >= _METADATA_MAX_ATTEMPTS:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                try:
                    provider_delay = max(0.0, float(error.retry_after or 0))
                except (TypeError, ValueError, OverflowError):
                    provider_delay = 0.0
                exponential_delay = 0.5 * (2 ** (attempt - 1))
                base_delay = max(provider_delay, exponential_delay)
                jitter = random.uniform(0.0, min(2.0, base_delay * 0.25))
                delay = base_delay + jitter
                if delay >= remaining:
                    raise
                if cancellation.wait(delay):
                    return []

    def _extract_request(
        self, request: dict[str, Any], cancellation: threading.Event | None = None
    ) -> list[dict[str, Any]]:
        use_bilibili_provider = (
            self._uses_bilibili_creator_provider(request)
            or self._uses_bilibili_search_provider(request)
            or self._uses_bilibili_detail_provider(request)
        )
        use_tiktok_provider = (
            self._uses_tiktok_creator_provider(request)
            or self._uses_tiktok_detail_provider(request)
        )
        use_douyin_provider = self._uses_douyin_search_provider(request)
        if not use_bilibili_provider and not use_tiktok_provider and not use_douyin_provider:
            from .acquisition_process import extract_metadata

            return extract_metadata(request, cancellation or threading.Event())
        try:
            stop_event = cancellation or threading.Event()
            deadline_seconds = float(
                (request.get("limits") or {}).get("deadline_seconds", 300)
            )
            if self._uses_bilibili_search_provider(request):
                operation = self._extract_bilibili_search(request)
            elif self._uses_bilibili_detail_provider(request):
                operation = self._extract_bilibili_detail(request)
            elif self._uses_bilibili_creator_provider(request):
                operation = self._extract_bilibili_creator(request)
            elif self._uses_tiktok_detail_provider(request):
                operation = self._extract_tiktok_detail(request)
            elif self._uses_douyin_search_provider(request):
                operation = self._extract_douyin_search(request)
            else:
                operation = self._extract_tiktok_creator(request)
            return asyncio.run(
                self._await_provider_operation(
                    operation, stop_event, deadline_seconds
                )
            )
        except AcquisitionError:
            raise
        except Exception as exc:
            from ..crawlers.runtime import CrawlerFailure

            if isinstance(exc, CrawlerFailure):
                raise self._map_crawler_failure(exc) from exc
            source_id = str(request.get("source_id") or "")
            if not source_id:
                source_id = self._source_for_url(
                    str((request.get("targets") or [""])[0])
                ) or "nguồn"
            raise AcquisitionError(
                f"Không lấy được metadata từ {source_id}.",
                code="SOURCE_UNAVAILABLE",
            ) from exc

    def _provider_for_request(self, request: dict[str, Any]) -> str:
        if request.get("provider_id"):
            return str(request["provider_id"])
        if (
            self._uses_bilibili_creator_provider(request)
            or self._uses_bilibili_search_provider(request)
            or self._uses_bilibili_detail_provider(request)
        ):
            return "cbce_bilibili"
        if self._uses_tiktok_creator_provider(request) or self._uses_tiktok_detail_provider(
            request
        ):
            return "tiktok_display"
        if self._uses_douyin_search_provider(request):
            return "cbce_douyin"
        return "yt-dlp"

    def capabilities(self) -> dict[str, Any]:
        """Return the acquisition operation matrix exposed to the UI.

        ``configured`` is intentionally separate from ``ready``: a browser
        connector can be installed while its live DOM operation still needs
        a canary. The frontend must only enable operations marked ready; the
        backend remains the authoritative validator for every run.
        """
        connector = self._bilibili_connector
        tiktok_connector = self._tiktok_connector
        douyin_connector = self._connectors.get("douyin")
        bilibili_configured = bool(
            connector is not None and getattr(connector, "configured", False)
        )
        bilibili_search_ready = self._uses_bilibili_search_provider(
            {"mode": "search", "source_id": "bilibili", "query": "capability"}
        )
        bilibili_creator_configured = bool(
            bilibili_configured
            and callable(getattr(connector, "list_creator", None))
        )
        session_bridge_ready = bool(
            bilibili_configured and callable(getattr(connector, "for_account", None))
        )
        tiktok_configured = bool(
            tiktok_connector is not None
            and getattr(tiktok_connector, "configured", False)
            and callable(getattr(tiktok_connector, "scan_channel", None))
            and callable(getattr(tiktok_connector, "fetch_detail", None))
        )
        douyin_spec = getattr(douyin_connector, "spec", None)
        douyin_provider_wired = bool(
            douyin_connector is not None
            and str(
                getattr(douyin_connector, "provider_id", None)
                or getattr(douyin_spec, "provider_id", "")
            ).strip()
            == "cbce_douyin"
        )
        douyin_search_ready = self._uses_douyin_search_provider(
            {"mode": "search", "source_id": "douyin", "query": "capability"}
        )

        def operation(
            status: str,
            provider_id: str | None,
            detail: str,
            reason_code: str | None = None,
            *,
            target_kinds: tuple[str, ...] = (),
            auth: str | None = None,
            schedule_policy: str | None = None,
            coverage: str | None = None,
            limits: Mapping[str, int] | None = None,
        ) -> dict[str, Any]:
            resolved_auth = auth or (
                "browser_session"
                if provider_id in {"cbce_bilibili", "cbce_douyin"}
                else "authorized_oauth"
                if provider_id == "tiktok_display"
                else "public_or_user_cookies"
                if provider_id == "yt-dlp"
                else "none"
            )
            resolved_schedule = schedule_policy or (
                "background_safe" if status == "ready" else "manual_only"
            )
            resolved_coverage = coverage or {
                "ready": "canary_verified",
                "unverified": "metadata_unverified",
                "setup_required": "not_configured",
                "unsupported": "none",
            }.get(status, "unknown")
            return {
                "status": status,
                "enabled": status == "ready",
                "provider_id": provider_id,
                "detail": detail,
                "reason_code": reason_code,
                "implementation": provider_id or "none",
                "availability": status,
                "target_kinds": list(target_kinds),
                "auth": resolved_auth,
                "schedule_policy": resolved_schedule,
                "coverage": resolved_coverage,
                "limits": dict(
                    limits
                    or {
                        "max_candidates": 100,
                        "max_requests": 20,
                        "deadline_seconds": 600,
                    }
                ),
                "implementation_version": "acquisition.v1",
                "checked_at": self._now(),
            }

        youtube_operations = {
            "video": operation("ready", "yt-dlp", "Metadata và tải link video đã qua canary."),
            "creator": operation("ready", "yt-dlp", "Creator enumeration đã qua canary."),
            "playlist": operation("ready", "yt-dlp", "Playlist enumeration đã qua canary."),
            "search": operation("ready", "yt-dlp", "Search metadata đã qua canary."),
            "download": operation("ready", "yt-dlp", "Download publication đã qua canary."),
        }
        bilibili_operations = {
            "video": operation("ready", "yt-dlp", "Link video và multipart đã qua canary."),
            "creator": operation(
                "unverified" if bilibili_creator_configured else "setup_required",
                "cbce_bilibili" if bilibili_creator_configured else None,
                (
                    "CBCE đã cấu hình nhưng creator list còn chờ live DOM hydrate gate."
                    if bilibili_creator_configured
                    else "Cần CBCE Bilibili và browser session."
                ),
                "BILIBILI_CREATOR_LIVE_GATE" if bilibili_creator_configured else "CBCE_NOT_READY",
            ),
            "playlist": operation(
                "ready",
                "yt-dlp",
                "Playlist Bilibili công khai đã qua ba canary metadata và enrichment title.",
            ),
            "search": operation(
                "ready" if bilibili_search_ready else "setup_required",
                "cbce_bilibili" if bilibili_search_ready else None,
                (
                    "CBCE Bilibili search đã qua canary."
                    if bilibili_search_ready
                    else "Cần provider CBCE Bilibili và browser session."
                ),
                None if bilibili_search_ready else "CBCE_NOT_READY",
            ),
            "download": operation("ready", "yt-dlp", "Download publication multipart đã qua canary."),
        }
        tiktok_operations = {
            "video": operation(
                "unverified" if tiktok_configured else "setup_required",
                "tiktok_display" if tiktok_configured else None,
                (
                    "TikTok Display detail chỉ dành cho video của creator đã cấp quyền; "
                    "chưa có media download contract."
                    if tiktok_configured
                    else "Cần TikTok Display API OAuth với scope video.list."
                ),
                "TIKTOK_DISPLAY_LIVE_GATE" if tiktok_configured else "TIKTOK_NOT_CONFIGURED",
            ),
            "creator": operation(
                "unverified" if tiktok_configured else "setup_required",
                "tiktok_display" if tiktok_configured else None,
                (
                    "TikTok Display chỉ quét creator đã cấp quyền; cần live gate metadata."
                    if tiktok_configured
                    else "Cần TikTok Display API OAuth với scope video.list."
                ),
                "TIKTOK_DISPLAY_LIVE_GATE" if tiktok_configured else "TIKTOK_NOT_CONFIGURED",
            ),
            "playlist": operation(
                "unsupported",
                None,
                "TikTok Display API không cung cấp playlist acquisition.",
                "TIKTOK_PLAYLIST_UNSUPPORTED",
            ),
            "search": operation(
                "unsupported",
                None,
                "TikTok Display API không cung cấp global search.",
                "TIKTOK_SEARCH_UNSUPPORTED",
            ),
            "download": operation(
                "unsupported",
                None,
                "TikTok Display API chưa cung cấp media download contract.",
                "TIKTOK_DOWNLOAD_UNSUPPORTED",
            ),
        }
        douyin_search_status = (
            "unverified"
            if douyin_search_ready
            else "setup_required"
            if douyin_provider_wired
            else "unsupported"
        )
        douyin_operations = {
            "video": operation(
                "unsupported",
                None,
                "Douyin detail chưa có acquisition provider riêng.",
                "DOUYIN_DETAIL_UNSUPPORTED",
            ),
            "creator": operation(
                "unsupported",
                None,
                "Douyin creator chưa có acquisition provider riêng.",
                "DOUYIN_CREATOR_UNSUPPORTED",
            ),
            "playlist": operation(
                "unsupported",
                None,
                "Douyin playlist chưa được hỗ trợ.",
                "DOUYIN_PLAYLIST_UNSUPPORTED",
            ),
            "search": operation(
                douyin_search_status,
                "cbce_douyin" if douyin_search_ready else None,
                (
                    "CBCE Douyin search đã nối nhưng còn chờ live DOM gate."
                    if douyin_search_ready
                    else "Cần CBCE Douyin và reviewed DOM contract."
                    if douyin_provider_wired
                    else "Douyin search chưa có reviewed CBCE contract sẵn sàng."
                ),
                "DOUYIN_SEARCH_LIVE_GATE"
                if douyin_search_ready
                else "CBCE_NOT_READY"
                if douyin_provider_wired
                else "DOUYIN_SEARCH_UNSUPPORTED",
            ),
            "download": operation(
                "unsupported",
                None,
                "Douyin chưa có media download contract riêng.",
                "DOUYIN_DOWNLOAD_UNSUPPORTED",
            ),
        }
        unsupported = {
            source_id: {
                "source_id": source_id,
                "label": label,
                "operations": {
                    mode: operation(
                        "unsupported",
                        None,
                        "Provider acquisition chưa được triển khai và chưa có live gate.",
                        "PROVIDER_NOT_IMPLEMENTED",
                    )
                    for mode in ("video", "creator", "playlist", "search", "download")
                },
            }
            for source_id, label in (
                ("xhs", "Xiaohongshu"),
            )
        }
        target_kinds_by_mode = {
            "video": ["content_url"],
            "creator": ["creator_url"],
            "playlist": ["playlist_url"],
            "search": ["keyword"],
            "download": ["content_url"],
        }
        for source in (
            youtube_operations,
            bilibili_operations,
            tiktok_operations,
            douyin_operations,
            *(item["operations"] for item in unsupported.values()),
        ):
            for mode, operation_value in source.items():
                operation_value["target_kinds"] = target_kinds_by_mode.get(mode, [])
        return {
            "version": 1,
            "feature_enabled": bool(settings.content_bot_acquisition_enabled),
            "feature_disabled_reason": (
                None
                if settings.content_bot_acquisition_enabled
                else "FEATURE_DISABLED"
            ),
            "generated_at": self._now(),
            "session_bridge": {
                "status": (
                    "unverified"
                    if session_bridge_ready
                    else "setup_required"
                ),
                "enabled": session_bridge_ready,
                "reason_code": (
                    "BILIBILI_SESSION_BRIDGE_LIVE_GATE"
                    if session_bridge_ready
                    else "CBCE_NOT_READY"
                ),
                "detail": (
                    "Connection profile Bilibili được cô lập theo từng connection_id; live login/DOM gate vẫn là điều kiện vận hành."
                    if session_bridge_ready
                    else "Cần provider CBCE Bilibili để dùng connection profile; tải ngoài bridge vẫn dùng cookie tạm thời."
                ),
            },
            "items": [
                {
                    "source_id": "youtube",
                    "label": "YouTube",
                    "operations": youtube_operations,
                },
                {
                    "source_id": "bilibili",
                    "label": "Bilibili",
                    "operations": bilibili_operations,
                },
                {
                    "source_id": "tiktok",
                    "label": "TikTok",
                    "operations": tiktok_operations,
                },
                {
                    "source_id": "douyin",
                    "label": "Douyin",
                    "operations": douyin_operations,
                },
                *unsupported.values(),
            ],
        }

    @classmethod
    def _extract_with_yt_dlp(cls, request: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise AcquisitionError(
                "Backend chưa có yt-dlp để lấy metadata.", code="DEPENDENCY_MISSING"
            ) from exc

        mode = str(request["mode"])
        limits = request.get("limits") or {}
        max_candidates = min(max(int(limits.get("max_candidates", 20)), 1), 100)
        deadline_seconds = min(max(int(limits.get("deadline_seconds", 300)), 30), 600)
        targets = list(request.get("targets") or [])
        listing_offset = int(request.get("listing_offset", 0))
        if not 0 <= listing_offset <= 5000:
            raise AcquisitionError("Vị trí phân trang vượt giới hạn.", code="BUDGET_EXCEEDED")
        target_source = cls._source_for_url(targets[0]) if len(targets) == 1 else None
        continuation_supported = (
            mode in {"creator", "playlist"}
            and len(targets) == 1
            and (
                target_source == "youtube"
                or (mode == "playlist" and target_source == "bilibili")
            )
        )
        if listing_offset and not continuation_supported:
            raise AcquisitionError("Provider chưa hỗ trợ cào tiếp target này.", code="UNSUPPORTED_OPERATION")
        if mode == "search":
            query = str(request.get("query") or "").strip()
            targets = [f"ytsearch{max_candidates}:{query}"]

        results: list[dict[str, Any]] = []
        started = time.monotonic()
        for target in targets:
            if time.monotonic() - started > deadline_seconds:
                break
            fallback_source = cls._source_for_url(target) or str(request.get("source_id") or "youtube")
            opts: dict[str, Any] = {
                "ignoreconfig": True,
                "proxy": "",
                "enable_file_urls": False,
                "cachedir": False,
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "socket_timeout": min(30, deadline_seconds),
                "retries": 1,
                "playliststart": listing_offset + 1,
                "playlistend": listing_offset + max_candidates,
                "noplaylist": mode == "video",
                "extract_flat": mode != "video",
            }
            try:
                with yt_dlp.YoutubeDL(opts) as downloader:
                    info = downloader.extract_info(target, download=False)
            except Exception as exc:
                message = str(exc).casefold()
                if any(word in message for word in ("login", "sign in", "private", "members")):
                    raise AcquisitionError(
                        "Nguồn yêu cầu đăng nhập hoặc quyền truy cập.", code="AUTH_REQUIRED"
                    ) from exc
                if "unsupported url" in message:
                    raise AcquisitionError(
                        "Provider chưa hỗ trợ loại link này.", code="UNSUPPORTED_OPERATION"
                    ) from exc
                raise AcquisitionError(
                    "Không lấy được metadata từ nguồn. Hãy thử lại hoặc kiểm tra link.",
                    code="SOURCE_UNAVAILABLE",
                ) from exc
            if not isinstance(info, dict):
                continue
            entries = cls._yt_dlp_entries(info, max_candidates=max_candidates - len(results))
            playlist_metadata = (
                cls._bilibili_playlist_metadata(
                    target,
                    max_candidates=max_candidates,
                    deadline_seconds=deadline_seconds,
                    started=started,
                    listing_offset=listing_offset,
                )
                if mode == "playlist" and fallback_source == "bilibili"
                else {}
            )
            for index, entry in enumerate(entries, start=len(results) + 1):
                entry_for_normalize = dict(entry)
                entry_id = str(
                    entry_for_normalize.get("id")
                    or entry_for_normalize.get("display_id")
                    or ""
                ).strip()
                enriched = playlist_metadata.get(entry_id)
                if enriched:
                    for key, value in enriched.items():
                        if not entry_for_normalize.get(key) and value not in (None, ""):
                            entry_for_normalize[key] = value
                # Preserve a Bilibili ``p`` selector from an exact input even
                # when the extractor returns a canonical URL without it.
                if mode == "video":
                    entry_for_normalize["webpage_url"] = target
                raw_entry_url = str(
                    entry.get("webpage_url")
                    or entry.get("original_url")
                    or entry.get("url")
                    or ""
                ).strip()
                fallback_url = (
                    raw_entry_url
                    if cls._source_for_url(raw_entry_url) is not None
                    else ""
                )
                if mode == "search" and not fallback_url and fallback_source == "youtube":
                    video_id = entry_id or raw_entry_url
                    if 6 <= len(video_id) <= 64 and all(
                        char.isalnum() or char in "_-" for char in video_id
                    ):
                        fallback_url = f"https://www.youtube.com/watch?v={video_id}"
                elif not fallback_url and mode in {"creator", "playlist"}:
                    if fallback_source == "youtube" and 6 <= len(entry_id) <= 64 and all(
                        char.isalnum() or char in "_-" for char in entry_id
                    ):
                        fallback_url = f"https://www.youtube.com/watch?v={entry_id}"
                    elif fallback_source == "bilibili" and re.fullmatch(
                        r"(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)", entry_id
                    ):
                        fallback_url = f"https://www.bilibili.com/video/{entry_id}"
                if mode in {"creator", "playlist"} and not fallback_url:
                    # A flat playlist entry without a content URL/ID is not a
                    # safe candidate; never fall back to the creator/playlist
                    # page itself and accidentally enqueue it for download.
                    continue
                results.append(
                    cls._normalize_entry(
                        entry_for_normalize,
                        fallback_url=target if mode == "video" else fallback_url,
                        fallback_source=fallback_source,
                        position=index,
                    )
                )
                if mode in {"creator", "playlist"}:
                    results[-1]["listing_position"] = int(entry.get("playlist_index") or listing_offset + index)
                if len(results) >= max_candidates:
                    break
            if len(results) >= max_candidates:
                break
        return results

    def _save_run(self, run: dict[str, Any], **changes: Any) -> dict[str, Any]:
        run.update(changes, updated_at=self._now())
        # Child summaries are a read projection, never a second source of state.
        return self.store.upsert_acquisition_document(
            RUNS, {key: value for key, value in run.items() if key not in {"children", "can_continue"}}
        )

    def _supports_continuation(self, run: dict[str, Any]) -> bool:
        request = run.get("request") or {}
        targets = request.get("targets") or []
        source_id = self._source_for_url(targets[0]) if len(targets) == 1 else None
        return bool(
            not run.get("parent_run_id") and not run.get("child_run_ids")
            and not request.get("subscription_id")
            and request.get("mode") in {"creator", "playlist"}
            and len(targets) == 1
            and (
                source_id == "youtube"
                or (request.get("mode") == "playlist" and source_id == "bilibili")
            )
            and run.get("provider_id") == "yt-dlp"
        )

    def _extract_continuation_page(
        self, run: dict[str, Any], event: threading.Event
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int], str]:
        """Bounded position-window scan; checkpoint advances only after ingestion.

        YouTube/Bilibili lists may reorder between requests. Stable IDs
        deduplicate the collected window; this cursor does not claim a
        consistent source snapshot.
        """
        request = run["request"]
        limits = request["limits"]
        source_id = self._source_for_url(str((request.get("targets") or [""])[0]))
        pagination = deepcopy(run.get("pagination") or {
            "kind": "bilibili_position_v1" if source_id == "bilibili" else "youtube_position_v1",
            "generation": 0,
            "next_offset": 0, "seen_ids": [], "exhausted": False,
        })
        seen = set(pagination["seen_ids"])
        entries = []
        scanned = duplicates = filtered = empty_rounds = 0
        deadline = time.monotonic() + limits["deadline_seconds"]
        reason = "page_budget"
        for _ in range(limits["max_pages"]):
            if event.is_set():
                break
            if pagination["next_offset"] >= 5000:
                reason = "scan_limit"
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                reason = "deadline"
                break
            remaining = limits["max_candidates"] - len(entries)
            size = min(remaining, 5000 - pagination["next_offset"])
            page_request = {**request, "listing_offset": pagination["next_offset"],
                            "limits": {**limits, "max_candidates": size, "deadline_seconds": remaining_time}}
            page = self._extract_with_retry(page_request, event)[:size]
            scanned += len(page)
            previous_count = len(entries)
            offset = pagination["next_offset"]
            pagination["next_offset"] = min(5000, max(
                offset + len(page),
                max((int(item.get("listing_position", 0)) for item in page), default=offset),
            ))
            for item in page:
                identity = str(item["_id"])
                if identity in seen:
                    duplicates += 1
                    continue
                seen.add(identity)
                if not self._matches_filters(item, request.get("filters") or {}):
                    filtered += 1
                    continue
                entries.append(item)
            if len(page) < size:
                pagination["exhausted"] = True
                reason = "exhausted"
                break
            if len(entries) >= limits["max_candidates"]:
                reason = "candidate_budget"
                break
            empty_rounds = empty_rounds + 1 if len(entries) == previous_count else 0
            if empty_rounds >= 2:
                reason = "no_new_items"
                pagination["exhausted"] = True
                break
        pagination["seen_ids"] = sorted(seen)
        pagination["last_stop_reason"] = reason
        return entries, pagination, {"scanned": scanned, "duplicate": duplicates, "filtered": filtered}, reason

    def _fail_subscription(
        self, subscription_id: Any, error: str, *, retry_after: int | None = None
    ) -> None:
        if not isinstance(subscription_id, str) or not subscription_id:
            return
        channel = self.store.acquisition_document(CHANNELS, subscription_id)
        if channel is None:
            return
        subscription = dict(channel.get("subscription") or {})
        delay_seconds = (
            30 * 60
            if retry_after is None
            else max(1, min(7 * 24 * 60 * 60, int(retry_after)))
        )
        subscription.update(
            active_run_id=None,
            lease_expires_at=None,
            reconcile_run_id=None,
            reconcile_lease_token=None,
            reconcile_lease_expires_at=None,
            last_status="failed",
            last_error=error[:500],
            next_run_at=(datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat(),
        )
        channel["subscription"] = subscription
        channel["updated_at"] = self._now()
        self.store.upsert_acquisition_document(CHANNELS, channel)

    def _validate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = deepcopy(payload)
        mode = str(request.get("mode") or "").strip().casefold()
        if mode not in MODES:
            raise AcquisitionError("Chế độ cào không hợp lệ.")
        request["mode"] = mode
        raw_connection_id = request.get("connection_id")
        connection_id = ""
        if raw_connection_id is not None:
            if not isinstance(raw_connection_id, str):
                raise AcquisitionError("connection_id phải là chuỗi.")
            if raw_connection_id.strip():
                try:
                    connection_id = normalize_account_ref(raw_connection_id)
                except (TypeError, ValueError) as exc:
                    raise AcquisitionError("connection_id không hợp lệ.") from exc
                if len(connection_id) > 128:
                    raise AcquisitionError("connection_id tối đa 128 ký tự.")
                request["connection_id"] = connection_id
            else:
                request.pop("connection_id", None)
        requested_provider = str(request.get("provider_id") or "").strip()
        if requested_provider and requested_provider not in {
            "yt-dlp",
            "cbce_bilibili",
            "cbce_douyin",
            "tiktok_display",
        }:
            raise AcquisitionError(
                "Provider acquisition chưa được đăng ký.",
                code="UNSUPPORTED_OPERATION",
            )
        if requested_provider:
            request["provider_id"] = requested_provider
        targets = [str(value).strip() for value in request.get("targets") or [] if str(value).strip()]
        if len(targets) > 20:
            raise AcquisitionError("Mỗi lượt chỉ nhận tối đa 20 target.", code="BUDGET_EXCEEDED")
        if mode == "search":
            query = str(request.get("query") or "").strip()
            if len(query) < 2 or len(query) > 180:
                raise AcquisitionError("Từ khóa phải dài từ 2 đến 180 ký tự.")
            source_id = str(request.get("source_id") or "youtube").casefold()
            if source_id not in {"youtube", "bilibili", "douyin"}:
                raise AcquisitionError(
                    "Tìm kiếm acquisition hiện chỉ bật cho YouTube, Bilibili CBCE hoặc Douyin CBCE.",
                    code="UNSUPPORTED_OPERATION",
                )
            request["source_id"] = source_id
            request["query"] = query
            if source_id == "bilibili" and not self._uses_bilibili_search_provider(request):
                raise AcquisitionError(
                    "Bilibili search cần provider CBCE và browser session sẵn sàng.",
                    code="UNSUPPORTED_OPERATION",
                )
            if source_id == "douyin" and not self._uses_douyin_search_provider(request):
                raise AcquisitionError(
                    "Douyin search cần provider CBCE và reviewed DOM contract sẵn sàng.",
                    code="UNSUPPORTED_OPERATION",
                )
            if requested_provider == "tiktok_display":
                raise AcquisitionError(
                    "TikTok Display không hỗ trợ global search.",
                    code="UNSUPPORTED_OPERATION",
                )
        elif not targets:
            raise AcquisitionError("Cần ít nhất một link target.")
        if requested_provider == "cbce_bilibili" and not (
            self._uses_bilibili_creator_provider(request)
            or self._uses_bilibili_search_provider(request)
            or self._uses_bilibili_detail_provider(request)
        ):
            raise AcquisitionError(
                "Provider CBCE Bilibili không khớp operation hoặc chưa sẵn sàng.",
                code="UNSUPPORTED_OPERATION",
            )
        if requested_provider == "cbce_douyin" and not self._uses_douyin_search_provider(
            request
        ):
            raise AcquisitionError(
                "Provider CBCE Douyin chỉ dùng cho search Douyin đã sẵn sàng.",
                code="UNSUPPORTED_OPERATION",
            )
        if requested_provider == "yt-dlp" and mode == "search" and request.get("source_id") == "bilibili":
            raise AcquisitionError(
                "Bilibili search chỉ được chạy qua provider CBCE.",
                code="UNSUPPORTED_OPERATION",
            )
        if requested_provider == "yt-dlp" and mode == "search" and request.get("source_id") == "douyin":
            raise AcquisitionError(
                "Douyin search chỉ được chạy qua provider CBCE.",
                code="UNSUPPORTED_OPERATION",
            )
        if mode == "video" and len(targets) > 20:
            raise AcquisitionError("Chế độ video chỉ nhận tối đa 20 link.", code="BUDGET_EXCEEDED")
        target_sources: set[str] = set()
        for target in targets:
            try:
                normalized = normalize_video_url(target)
            except ValueError as exc:
                raise AcquisitionError("Target phải là link http/https hợp lệ.") from exc
            source_id = self._source_for_url(normalized)
            if source_id is None:
                raise AcquisitionError(
                    "Chưa nhận diện được nền tảng của target.", code="UNSUPPORTED_OPERATION"
                )
            target_sources.add(source_id)
        if target_sources - {"youtube", "bilibili", "tiktok"}:
            unsupported = ", ".join(sorted(target_sources - {"youtube", "bilibili", "tiktok"}))
            raise AcquisitionError(
                f"Acquisition hiện chưa mở operation cho: {unsupported}.",
                code="UNSUPPORTED_OPERATION",
            )
        if "tiktok" in target_sources:
            if mode == "creator":
                if len(targets) != 1:
                    raise AcquisitionError(
                        "TikTok creator hiện chỉ nhận một target mỗi lượt.",
                        code="UNSUPPORTED_OPERATION",
                    )
                if not self._uses_tiktok_creator_provider(request):
                    raise AcquisitionError(
                        "TikTok creator cần TikTok Display API của creator đã cấp quyền; không fallback sang yt-dlp.",
                        code="UNSUPPORTED_OPERATION",
                    )
            elif mode == "video":
                if not self._uses_tiktok_detail_provider(request):
                    raise AcquisitionError(
                        "TikTok video cần TikTok Display API đã cấu hình; không fallback sang yt-dlp.",
                        code="UNSUPPORTED_OPERATION",
                    )
            else:
                raise AcquisitionError(
                    "TikTok Display hiện chỉ hỗ trợ detail video và creator đã cấp quyền.",
                    code="UNSUPPORTED_OPERATION",
                )
        elif requested_provider == "tiktok_display":
            raise AcquisitionError(
                "Provider TikTok Display chỉ dùng cho target TikTok.",
                code="UNSUPPORTED_OPERATION",
            )
        if "tiktok" in target_sources and requested_provider == "yt-dlp":
            raise AcquisitionError(
                "TikTok không được fallback sang yt-dlp.",
                code="UNSUPPORTED_OPERATION",
            )
        if mode == "creator" and "bilibili" in target_sources:
            if not self._uses_bilibili_creator_provider(request):
                raise AcquisitionError(
                    "Bilibili creator cần provider CBCE đã cấu hình; không fallback sang yt-dlp.",
                    code="UNSUPPORTED_OPERATION",
                )
            if len(targets) != 1:
                raise AcquisitionError(
                    "Bilibili creator hiện chỉ nhận một target mỗi lượt.",
                    code="UNSUPPORTED_OPERATION",
                )
        if connection_id:
            connection_supported = (
                mode == "search"
                and request.get("source_id") == "bilibili"
            ) or (
                mode == "creator"
                and target_sources == {"bilibili"}
            ) or (
                mode in {"video", "playlist"}
                and target_sources == {"bilibili"}
            )
            if not connection_supported:
                raise AcquisitionError(
                    "session bridge hiện chỉ hỗ trợ video/creator/search Bilibili qua CBCE.",
                    code="UNSUPPORTED_OPERATION",
                )
            # A direct Bilibili video can still use the downloader profile
            # bridge when CBCE detail is unavailable. If CBCE is ready, the
            # extraction path above upgrades preview to the same profile.
            provider_ready = mode in {"video", "playlist"} or (
                self._uses_bilibili_search_provider(request)
                if mode == "search"
                else self._uses_bilibili_creator_provider(request)
            )
            if not provider_ready:
                raise AcquisitionError(
                    "Bilibili connection profile cần provider CBCE và browser session sẵn sàng.",
                    code="UNSUPPORTED_OPERATION",
                )
        request["targets"] = targets
        limits = request.get("limits") if isinstance(request.get("limits"), dict) else {}
        request["limits"] = {
            "max_candidates": min(max(int(limits.get("max_candidates", 20)), 1), 100),
            "max_pages": min(max(int(limits.get("max_pages", 20)), 1), 20),
            "deadline_seconds": min(max(int(limits.get("deadline_seconds", 300)), 30), 600),
        }
        raw_filters = request.get("filters") if isinstance(request.get("filters"), dict) else {}
        allowed_filters = {
            "media_type",
            "only_not_downloaded",
            "title_contains",
            "creator_id",
            "min_duration_seconds",
            "max_duration_seconds",
            "published_after",
        }
        unknown_filters = sorted(set(raw_filters) - allowed_filters)
        if unknown_filters:
            raise AcquisitionError(
                f"Bộ lọc chưa được hỗ trợ: {', '.join(unknown_filters[:3])}.",
                code="UNSUPPORTED_OPERATION",
            )
        filters: dict[str, Any] = {}
        media_type = str(raw_filters.get("media_type") or "").strip().casefold()
        if media_type:
            if media_type not in {"video", "audio", "live"}:
                raise AcquisitionError("Bộ lọc media_type không hợp lệ.")
            filters["media_type"] = media_type
        filters["only_not_downloaded"] = bool(raw_filters.get("only_not_downloaded", False))
        for key in ("title_contains", "creator_id", "published_after"):
            value = raw_filters.get(key)
            if value is not None and str(value).strip():
                text = str(value).strip()
                if len(text) > (120 if key != "published_after" else 64):
                    raise AcquisitionError(f"Bộ lọc {key} quá dài.")
                if key == "published_after" and self._parse_iso(text) is None:
                    raise AcquisitionError("Bộ lọc published_after phải là ISO datetime.")
                filters[key] = text
        for key in ("min_duration_seconds", "max_duration_seconds"):
            value = raw_filters.get(key)
            if value is None or value == "":
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise AcquisitionError(f"Bộ lọc {key} không hợp lệ.") from exc
            if number < 0 or number > 86_400:
                raise AcquisitionError(f"Bộ lọc {key} vượt giới hạn.")
            filters[key] = number
        if (
            filters.get("min_duration_seconds") is not None
            and filters.get("max_duration_seconds") is not None
            and filters["min_duration_seconds"] > filters["max_duration_seconds"]
        ):
            raise AcquisitionError("Khoảng thời lượng filter không hợp lệ.")
        request["filters"] = filters
        request.pop("cookie_text", None)
        return request

    def create_run(
        self, payload: dict[str, Any], *, run_id: str | None = None
    ) -> dict[str, Any]:
        self._ensure_feature_enabled()
        if payload.get("mode") in {"creator", "playlist"} and len(payload.get("targets") or []) > 1:
            return self._create_batch_run(payload, run_id=run_id)
        request = self._validate_request(payload)
        with self._lock:
            if not self._accepting:
                raise AcquisitionError("Ứng dụng đang đóng, chưa nhận lượt cào mới.", code="SERVICE_STOPPING")
            if not self.store.is_available:
                raise AcquisitionError(
                    "Kho dữ liệu chưa sẵn sàng; chưa nhận lượt cào mới.",
                    code="STORAGE_UNAVAILABLE",
                )
            self._check_queue_capacity()
            run_id = run_id or uuid.uuid4().hex
            run = {
                "_id": run_id,
                "mode": request["mode"],
                "provider_id": self._provider_for_request(request),
                "source_id": request.get("source_id"),
                "state": "queued",
                "phase": "queued",
                "stop_reason": None,
                "error_code": None,
                "error": None,
                "retry_after": None,
                "request": request,
                "counters": {
                    "scanned": 0,
                    "new": 0,
                    "duplicate": 0,
                    "filtered": 0,
                    "unavailable": 0,
                },
                "created_at": self._now(),
                "updated_at": self._now(),
            }
            saved = self.store.upsert_acquisition_document(RUNS, run)
            cancel_event = threading.Event()
            self._cancel_events[run_id] = cancel_event
            future = self._executor.submit(self._execute_run, run_id, cancel_event)
            self._futures[run_id] = future
            future.add_done_callback(lambda _, key=run_id: self._retire(key))
            return saved

    def _create_batch_run(
        self, payload: dict[str, Any], *, run_id: str | None = None
    ) -> dict[str, Any]:
        targets = list(dict.fromkeys(str(value).strip() for value in payload["targets"] if str(value).strip()))
        if len(payload["targets"]) > 20:
            raise AcquisitionError("Mỗi lượt chỉ nhận tối đa 20 target.", code="BUDGET_EXCEEDED")
        if len(targets) < 2:
            return self.create_run({**payload, "targets": targets}, run_id=run_id)
        prepared: list[tuple[str, dict[str, Any] | None, AcquisitionError | None]] = []
        for target in targets:
            try:
                child_request = self._validate_request({**payload, "targets": [target]})
                prepared.append((target, child_request, None))
            except AcquisitionError as error:
                if error.code != "UNSUPPORTED_OPERATION":
                    raise
                prepared.append((target, None, error))
        valid = next((request for _, request, _ in prepared if request is not None), None)
        if valid is None:
            error = prepared[0][2]
            assert error is not None
            raise error
        request = deepcopy(valid)
        request["targets"] = targets
        with self._lock:
            if not self._accepting:
                raise AcquisitionError("Ứng dụng đang đóng.", code="SERVICE_STOPPING")
            if not self.store.is_available:
                raise AcquisitionError("Kho dữ liệu chưa sẵn sàng.", code="STORAGE_UNAVAILABLE")
            self._check_queue_capacity()
            identifier = run_id or uuid.uuid4().hex
            child_ids = [f"{identifier}:{index}" for index in range(len(targets))]
            now = self._now()
            parent = {
                "_id": identifier, "mode": request["mode"], "provider_id": "batch",
                "source_id": None, "state": "queued", "phase": "queued",
                "request": request, "child_run_ids": child_ids,
                "stop_reason": None, "error_code": None, "error": None,
                "retry_after": None,
                "counters": {key: 0 for key in ("scanned", "new", "duplicate", "filtered", "unavailable")},
                "created_at": now, "updated_at": now,
            }
            # Publish the parent last: an interrupted setup never exposes a
            # runnable group whose children have not yet been persisted.
            for index, (target, child_request, error) in enumerate(prepared):
                child_request = child_request or {**deepcopy(valid), "targets": [target]}
                child = {
                    **deepcopy(parent), "_id": child_ids[index],
                    "parent_run_id": identifier, "request": child_request,
                    "source_id": self._source_for_url(target),
                    "provider_id": self._provider_for_request(child_request) if error is None else "unavailable",
                    "state": "failed" if error else "queued",
                    "phase": "failed" if error else "queued",
                    "error_code": error.code if error else None,
                    "error": str(error) if error else None,
                    "retry_after": error.retry_after if error else None,
                }
                child.pop("child_run_ids")
                self.store.upsert_acquisition_document(RUNS, child)
            self.store.upsert_acquisition_document(RUNS, parent)
            event = threading.Event()
            self._cancel_events[identifier] = event
            future = self._executor.submit(self._execute_run, identifier, event)
            self._futures[identifier] = future
            future.add_done_callback(lambda _, key=identifier: self._retire(key))
            return self.get_run(identifier)

    def _batch_candidates(self, run: dict[str, Any]) -> dict[str, int]:
        """Rebuild the parent union from durable child relations after replay."""
        child_ids = run["child_run_ids"]
        relations = [row for row in self.store.acquisition_documents(RUN_ITEMS)
                     if row.get("run_id") in child_ids]
        relations.sort(key=lambda row: (child_ids.index(row["run_id"]), int(row.get("position") or 0)))
        seen: set[str] = set()
        for relation in relations:
            candidate_id = relation["candidate_id"]
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            self.store.upsert_acquisition_document(RUN_ITEMS, {
                "_id": f"{run['id']}:{candidate_id}", "run_id": run["id"],
                "candidate_id": candidate_id, "position": len(seen),
                "seen_at": relation.get("seen_at") or self._now(),
            })
        children = [self.store.acquisition_document(RUNS, key) for key in child_ids]
        counters = {key: sum(int((child or {}).get("counters", {}).get(key, 0)) for child in children)
                    for key in ("scanned", "new", "duplicate", "filtered", "unavailable")}
        counters["new"] = len(seen)
        counters["duplicate"] += len(relations) - len(seen)
        return counters

    def _execute_batch_run(self, run: dict[str, Any], event: threading.Event) -> None:
        limits = run["request"]["limits"]
        deadline = time.monotonic() + limits["deadline_seconds"]
        counters = self._batch_candidates(run)
        remaining_targets = sum(
            (self.store.acquisition_document(RUNS, key) or {}).get("state") != "completed"
            for key in run["child_run_ids"]
        )
        for child_id in run["child_run_ids"]:
            if event.is_set():
                return
            child = self.store.acquisition_document(RUNS, child_id)
            if child is None:
                raise AcquisitionError("Thiếu lượt con trong nhóm cào.", code="INTERNAL_ERROR")
            if child["state"] == "completed":
                continue
            remaining_items = limits["max_candidates"] - counters["scanned"]
            remaining_seconds = deadline - time.monotonic()
            item_share = max(1, remaining_items // remaining_targets)
            time_share = remaining_seconds / remaining_targets
            remaining_targets -= 1
            if remaining_items <= 0 or remaining_seconds <= 0:
                self._save_executing_run(child, event, state="skipped", phase="skipped",
                                         stop_reason="batch_budget", error_code="BUDGET_EXCEEDED",
                                         error="Nhóm cào đã hết ngân sách metadata hoặc thời gian.")
                continue
            try:
                child_request = self._validate_request(child["request"])
            except AcquisitionError as error:
                self._save_executing_run(child, event, state="failed", phase="failed",
                                         error_code=error.code, error=str(error),
                                         retry_after=error.retry_after,
                                         stop_reason="provider_error")
                continue
            child_request["limits"] = {**child_request["limits"],
                                       "max_candidates": item_share,
                                       "deadline_seconds": time_share}
            if not self._save_executing_run(child, event, request=child_request,
                                           provider_id=self._provider_for_request(child_request)):
                return
            # Inline execution occupies the parent's one executor slot; it
            # cannot deadlock a one-worker pool or fan out profile owners.
            self._execute_run(child_id, event)
            counters = self._batch_candidates(run)
            if not self._save_executing_run(run, event, counters=counters):
                return
        children = [self.store.acquisition_document(RUNS, key) for key in run["child_run_ids"]]
        succeeded = sum(child["state"] == "completed" for child in children if child)
        complete = succeeded == len(children)
        budget_limited = any(child and child.get("stop_reason") == "candidate_budget" for child in children)
        self._save_executing_run(
            run, event, state="completed" if complete else "partial" if succeeded else "failed",
            phase="completed", counters=counters,
            stop_reason=("candidate_budget" if budget_limited else "exhausted") if complete else "partial_targets",
            error_code=None if complete else "TARGETS_INCOMPLETE",
            error=None if complete else f"{succeeded}/{len(children)} kênh hoàn tất. Xem trạng thái từng kênh.",
        )

    def _retire(self, run_id: str) -> None:
        with self._lock:
            self._futures.pop(run_id, None)
            self._cancel_events.pop(run_id, None)

    def _check_queue_capacity(self) -> None:
        # Caller holds _lock across admission and executor submission.
        if len(self._futures) >= self._max_pending:
            raise AcquisitionError(
                "Hàng đợi cào đã đầy. Vui lòng đợi lượt đang chạy hoàn tất.",
                code="QUEUE_FULL",
                retry_after=60,
            )

    def _save_executing_run(
        self, run: dict[str, Any], cancel_event: threading.Event, **changes: Any
    ) -> bool:
        with self._lock:
            if cancel_event.is_set():
                return False
            self._save_run(run, **changes)
            return True

    def _execute_run(self, run_id: str, cancel_event: threading.Event) -> None:
        run = self.store.acquisition_document(RUNS, run_id)
        if not run:
            return
        request = run.get("request") or {}
        try:
            if not self._save_executing_run(
                run, cancel_event, state="running", phase="collecting", error=None,
                error_code=None, retry_after=None
            ):
                return
            if run.get("child_run_ids"):
                self._execute_batch_run(run, cancel_event)
                return
            paged = self._supports_continuation(run)
            pagination = page_counters = None
            page_reason = None
            if paged:
                entries, pagination, page_counters, page_reason = self._extract_continuation_page(run, cancel_event)
            else:
                entries = self._extract_with_retry(request, cancel_event)
            if cancel_event.is_set():
                return
            candidate_limit = int(request.get("limits", {}).get("max_candidates", 20))
            reached_budget = len(entries) >= candidate_limit
            entries = entries[:candidate_limit]
            counters = dict(run.get("counters") or {})
            existing_relations = []
            if paged:
                existing_relations = [row for row in self.store.acquisition_documents(RUN_ITEMS)
                                      if row.get("run_id") == run_id]
                counters["new"] = len(existing_relations)
                for key, value in page_counters.items():
                    counters[key] = counters.get(key, 0) + value
            else:
                counters["scanned"] = len(entries)
            next_position = max((int(row.get("position") or 0) for row in existing_relations), default=0)
            for position, candidate in enumerate(entries, start=1):
                if cancel_event.is_set():
                    return
                candidate = deepcopy(candidate)
                candidate["position"] = position
                candidate["first_seen_at"] = candidate.get("first_seen_at") or self._now()
                previous = self.store.acquisition_document(CANDIDATES, candidate["_id"])
                candidate["updated_at"] = self._now()
                self.store.upsert_acquisition_document(CANDIDATES, candidate)
                if not self._matches_filters(candidate, request.get("filters") or {}):
                    counters["filtered"] = counters.get("filtered", 0) + 1
                    continue
                relation_id = f"{run_id}:{candidate['_id']}"
                relation = self.store.acquisition_document(RUN_ITEMS, relation_id)
                if relation is None:
                    next_position += 1
                    counters["new"] = counters.get("new", 0) + 1
                    self.store.upsert_acquisition_document(
                        RUN_ITEMS,
                        {
                            "_id": relation_id,
                            "run_id": run_id,
                            "candidate_id": candidate["_id"],
                            "position": next_position if paged else position,
                            "seen_at": self._now(),
                        },
                    )
                elif previous is not None and not paged:
                    counters["duplicate"] = counters.get("duplicate", 0) + 1
            if not self._save_executing_run(
                run, cancel_event,
                state="completed",
                phase="completed",
                stop_reason=page_reason if paged else "candidate_budget" if reached_budget else "exhausted" if entries else "no_results",
                counters=counters,
                **({"pagination": pagination} if paged else {}),
            ):
                return
            subscription_id = request.get("subscription_id")
            if isinstance(subscription_id, str) and subscription_id:
                try:
                    self._finalize_subscription(subscription_id, run_id)
                except Exception:
                    logger.exception("Could not finalize acquisition subscription: %s", subscription_id)
        except AcquisitionError as exc:
            if not self._save_executing_run(
                run, cancel_event,
                state="failed",
                phase="failed",
                error_code=exc.code,
                error=str(exc),
                retry_after=exc.retry_after,
                stop_reason="provider_error",
            ):
                return
            self._fail_subscription(
                request.get("subscription_id"), str(exc), retry_after=exc.retry_after
            )
        except Exception:
            if cancel_event.is_set():
                return
            logger.exception("Video acquisition run failed: %s", run_id)
            if not self._save_executing_run(
                run, cancel_event,
                state="failed",
                phase="failed",
                error_code="INTERNAL_ERROR",
                error="Không thể hoàn tất lượt cào. Hãy thử lại.",
                retry_after=None,
                stop_reason="internal_error",
            ):
                return
            self._fail_subscription(
                request.get("subscription_id"), "Không thể hoàn tất lượt theo dõi nguồn."
            )

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.store.acquisition_documents(RUNS) if not row.get("parent_run_id")]
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:min(max(limit, 1), 100)]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.acquisition_document(RUNS, run_id)
        if run:
            pagination = run.get("pagination") or {}
            run["can_continue"] = bool(
                self._supports_continuation(run) and run.get("state") == "completed"
                and pagination and not pagination.get("exhausted")
                and int(pagination.get("next_offset", 0)) < 5000
            )
        if run and run.get("child_run_ids"):
            run["children"] = [
                {"id": child["id"], "target": (child.get("request", {}).get("targets") or [""])[0],
                 **{key: child.get(key) for key in ("state", "provider_id", "error_code", "error", "retry_after", "counters", "stop_reason")}}
                for child_id in run["child_run_ids"]
                if (child := self.store.acquisition_document(RUNS, child_id)) is not None
            ]
        return run

    def continue_run(self, run_id: str, *, expected_generation: int) -> dict[str, Any] | None:
        self._ensure_feature_enabled()
        with self._lock:
            run = self.get_run(run_id)
            if run is None:
                return None
            self._require_parent_control(run)
            if not self._supports_continuation(run) or not run.get("pagination"):
                raise AcquisitionError(
                    "Cào tiếp hiện hỗ trợ creator/playlist YouTube và playlist Bilibili độc lập.",
                    code="UNSUPPORTED_OPERATION",
                )
            generation = int(run["pagination"]["generation"])
            if expected_generation < generation:
                return run
            if expected_generation != generation or not run["can_continue"]:
                raise AcquisitionError("Lượt cào chưa sẵn sàng hoặc đã hết nguồn/ngân sách.", code="RUN_NOT_CONTINUABLE")
            if not self._accepting:
                raise AcquisitionError("Ứng dụng đang đóng.", code="SERVICE_STOPPING")
            if run_id in self._futures:
                raise AcquisitionError("Lượt cào trước đang kết thúc.", code="RUN_STOPPING", retry_after=1, run_id=run_id)
            self._check_queue_capacity()
            claimed = self.store.advance_acquisition_pagination(
                run_id, expected_generation=expected_generation, now=self._now()
            )
            if claimed is None:
                current = self.get_run(run_id)
                if current and current.get("pagination", {}).get("generation", 0) > expected_generation:
                    return current
                raise AcquisitionError("Lượt cào đã thay đổi; hãy tải lại trạng thái.", code="RUN_NOT_CONTINUABLE")
            event = threading.Event()
            self._cancel_events[run_id] = event
            future = self._executor.submit(self._execute_run, run_id, event)
            self._futures[run_id] = future
            future.add_done_callback(lambda _, key=run_id: self._retire(key))
            return self.get_run(run_id)

    @staticmethod
    def _require_parent_control(run: dict[str, Any]) -> None:
        if run.get("parent_run_id"):
            raise AcquisitionError("Điều khiển lượt cào qua nhóm chứa kênh này.",
                                   code="UNSUPPORTED_OPERATION", run_id=run["parent_run_id"])

    def _stop_batch_children(self, run: dict[str, Any], state: str, reason: str) -> None:
        for child_id in run.get("child_run_ids", []):
            child = self.store.acquisition_document(RUNS, child_id)
            if child and child["state"] in ACTIVE_RUN_STATES:
                self._save_run(child, state=state, phase=state, stop_reason=reason)

    def list_candidates(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
        media_type: str | None = None,
        only_not_downloaded: bool = False,
    ) -> dict[str, Any]:
        if self.get_run(run_id) is None:
            raise AcquisitionError("Không tìm thấy lượt cào.", code="NOT_FOUND")
        relations = [
            row for row in self.store.acquisition_documents(RUN_ITEMS)
            if row.get("run_id") == run_id
        ]
        relations.sort(key=lambda row: int(row.get("position") or 0))
        candidates: list[dict[str, Any]] = []
        intents = self.store.acquisition_documents(INTENTS)
        intents_by_candidate: dict[str, list[dict[str, Any]]] = {}
        for row in intents:
            candidate_key = str(row.get("candidate_id") or "")
            if candidate_key:
                intents_by_candidate.setdefault(candidate_key, []).append(row)
        for relation in relations:
            candidate = self.store.acquisition_document(CANDIDATES, str(relation["candidate_id"]))
            if candidate is None:
                continue
            if media_type and candidate.get("media_type") != media_type:
                continue
            candidate_intents = intents_by_candidate.get(str(candidate["id"]), [])
            if only_not_downloaded and any(
                intent.get("state") in {"queued", "running", "succeeded"}
                for intent in candidate_intents
            ):
                continue
            intent = max(
                candidate_intents,
                key=lambda row: (
                    str(row.get("updated_at") or ""),
                    str(row.get("quality") or ""),
                ),
                default=None,
            )
            candidate["download"] = {
                "state": intent.get("state") if intent else "not_selected",
                "job_id": intent.get("job_id") if intent else None,
                "error": intent.get("error") if intent else None,
            }
            candidates.append(candidate)
        bounded_offset = max(offset, 0)
        bounded_limit = min(max(limit, 1), 100)
        return {
            "run_id": run_id,
            "items": candidates[bounded_offset:bounded_offset + bounded_limit],
            "total": len(candidates),
            "offset": bounded_offset,
            "limit": bounded_limit,
        }

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self.get_run(run_id)
            if run is None:
                return None
            self._require_parent_control(run)
            if run.get("state") in ACTIVE_RUN_STATES:
                event = self._cancel_events.get(run_id)
                if event:
                    event.set()
                self._stop_batch_children(run, "canceled", "user_canceled")
                return self._save_run(run, state="canceled", phase="canceled", stop_reason="user_canceled")
            return run

    def pause(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self.get_run(run_id)
            if run is None:
                return None
            self._require_parent_control(run)
            if run.get("state") in {"queued", "running"}:
                event = self._cancel_events.get(run_id)
                if event:
                    event.set()
                self._stop_batch_children(run, "paused", "user_paused")
                return self._save_run(run, state="paused", phase="paused", stop_reason="user_paused")
            return run

    def resume(self, run_id: str) -> dict[str, Any] | None:
        self._ensure_feature_enabled()
        with self._lock:
            if not self._accepting:
                raise AcquisitionError("Ứng dụng đang đóng.", code="SERVICE_STOPPING")
            run = self.get_run(run_id)
            if run is None:
                return None
            self._require_parent_control(run)
            if run.get("state") not in {"paused", "canceled", "failed", "partial"}:
                return run
            if run_id in self._futures:
                raise AcquisitionError(
                    "Lượt cào trước đang dừng. Vui lòng thử lại sau.",
                    code="RUN_STOPPING",
                    retry_after=1,
                    run_id=run_id,
                )
            self._check_queue_capacity()
            request = run.get("request") or {}
            run.update(
                state="queued",
                phase="queued",
                error=None,
                error_code=None,
                retry_after=None,
                stop_reason=None,
            )
            saved = self._save_run(run)
            event = threading.Event()
            self._cancel_events[run_id] = event
            future = self._executor.submit(self._execute_run, run_id, event)
            self._futures[run_id] = future
            future.add_done_callback(lambda _, key=run_id: self._retire(key))
            del request
            return saved

    def _selection_intents(self, selection: dict[str, Any]) -> list[dict[str, Any]]:
        intent_ids = {
            str(value) for value in (selection.get("intent_ids") or []) if str(value).strip()
        }
        all_intents = self.store.acquisition_documents(INTENTS)
        return (
            [
                row
                for row in all_intents
                if str(row.get("_id", row.get("id"))) in intent_ids
            ]
            if intent_ids
            else [row for row in all_intents if row.get("selection_id") == selection["id"]]
        )

    @staticmethod
    def _selection_state(selection: dict[str, Any], intents: list[dict[str, Any]]) -> str:
        if not intents:
            return str(selection.get("state") or "queued")
        states = {str(row.get("state") or "unknown") for row in intents}
        if states & {"queued", "running"}:
            return "queued"
        if "paused" in states:
            return "paused"
        if "needs_cookies" in states:
            return "needs_cookies"
        if "pending" in states:
            return "queued"
        if states & {"failed", "missing"}:
            return "failed"
        if states <= {"succeeded"}:
            return "completed"
        if states <= {"canceled"} or "canceled" in states:
            return "canceled"
        return str(selection.get("state") or "queued")

    def _selection_view(self, selection: dict[str, Any]) -> dict[str, Any]:
        self._refresh_intent_states()
        intents = self._selection_intents(selection)
        counts: dict[str, int] = {}
        for intent in intents:
            state = str(intent.get("state") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        output = deepcopy(selection)
        output["counts"] = counts
        output["total"] = len(intents)
        output["state"] = self._selection_state(selection, intents)
        return output

    def _refresh_intent_states(self) -> None:
        get_job = getattr(self.video_downloads, "get", None)
        if not callable(get_job):
            return
        published = getattr(self.video_downloads, "job_is_published", None)
        for intent in self.store.acquisition_documents(INTENTS):
            job_id = intent.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                continue
            job = get_job(job_id)
            if job is None:
                if intent.get("state") not in {"failed", "canceled", "missing"}:
                    intent.update(
                        state="missing",
                        error="Job tải không còn trong hàng đợi; có thể xếp lại.",
                        updated_at=self._now(),
                    )
                    self.store.upsert_acquisition_document(INTENTS, intent)
                continue
            state = str(job.get("state") or intent.get("state") or "unknown")
            if state == "succeeded" and callable(published) and not published(job_id):
                state = "missing"
            if state != intent.get("state") or job.get("error") != intent.get("error"):
                intent.update(state=state, error=job.get("error"), updated_at=self._now())
                self.store.upsert_acquisition_document(INTENTS, intent)

    def _download_queue_capacity(self) -> int:
        capacity = getattr(self.video_downloads, "queue_capacity", None)
        if callable(capacity):
            try:
                return max(0, int(capacity()))
            except (TypeError, ValueError, RuntimeError):
                logger.debug("Could not read downloader queue capacity", exc_info=True)
                return 0
        # Test doubles and older downloader implementations do not expose the
        # admission signal. Keep a conservative compatibility ceiling rather
        # than turning a durable selection into an unbounded submission.
        try:
            active = sum(
                1
                for row in self.video_downloads.list()
                if row.get("state") in {"queued", "running"}
            )
        except Exception:
            return 0
        return max(0, 32 - active)

    @staticmethod
    def _normalize_download_connection(
        connection_id: str | None,
    ) -> str | None:
        if connection_id is None or not str(connection_id).strip():
            return None
        if not isinstance(connection_id, str):
            raise AcquisitionError("connection_id phải là chuỗi.")
        try:
            normalized = normalize_account_ref(connection_id)
        except (TypeError, ValueError) as exc:
            raise AcquisitionError("connection_id không hợp lệ.") from exc
        if len(normalized) > 128:
            raise AcquisitionError("connection_id tối đa 128 ký tự.")
        return normalized

    @staticmethod
    def _download_intent_id(
        candidate_id: str,
        quality: str,
        connection_id: str | None,
    ) -> str:
        if not connection_id:
            return f"{candidate_id}:{quality}"
        connection_key = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:24]
        return f"{candidate_id}:{quality}:connection:{connection_key}"

    @staticmethod
    def _selection_id(idempotency_key: str | None) -> str:
        if idempotency_key:
            return hashlib.sha256(
                f"acquisition-selection\x00{idempotency_key}".encode()
            ).hexdigest()[:32]
        return uuid.uuid4().hex

    def _submit_downloads(
        self,
        urls: list[str],
        *,
        quality: str,
        cookie_text: str | None,
        connection_id: str | None,
        intent_keys: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "quality": quality,
            "cookie_text": cookie_text,
        }
        # Keep older test doubles and rolling deployments compatible while
        # routing real connection-bound jobs explicitly.
        if connection_id:
            kwargs["connection_id"] = connection_id
        if intent_keys:
            kwargs["intent_keys"] = intent_keys
        try:
            return self.video_downloads.submit(urls, **kwargs)
        except TypeError as exc:
            # Keep rolling test doubles/older downloader instances compatible
            # while the durable intent-aware bridge is deployed.
            if "intent_keys" not in str(exc):
                raise
            kwargs.pop("intent_keys", None)
            return self.video_downloads.submit(urls, **kwargs)

    def _attach_download_provenance(
        self, job_id: object, candidate: Mapping[str, Any]
    ) -> None:
        attach = getattr(self.video_downloads, "attach_provenance", None)
        if not callable(attach) or not isinstance(job_id, str) or not job_id:
            return
        attach(
            job_id,
            {
                "candidate_id": candidate.get("_id") or candidate.get("id"),
                "source_id": candidate.get("source_id"),
                "provider_id": candidate.get("provider_id"),
                "external_id": candidate.get("external_id"),
                "media_id": candidate.get("media_id"),
                "part_index": candidate.get("part_index"),
                "creator_id": candidate.get("creator_id"),
                "uploader": candidate.get("uploader"),
                "source_url": candidate.get("canonical_url"),
            },
        )

    def _retry_download(
        self,
        job_id: str,
        *,
        cookie_text: str | None,
        connection_id: str | None,
    ) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {"cookie_text": cookie_text}
        if connection_id:
            kwargs["connection_id"] = connection_id
        return self.video_downloads.retry(job_id, **kwargs)

    def _drain_pending_downloads(
        self,
        *,
        selection_id: str | None = None,
        intent_ids: set[str] | None = None,
        cookie_text: str | None = None,
    ) -> None:
        """Admit durable pending intents as downloader slots become free.

        A selection may be larger than the downloader's bounded queue. Items
        beyond the current admission window stay in storage as ``pending``
        (or ``needs_cookies`` when the original request used cookies) and are
        promoted by the scheduler or the selection polling path. No selected
        item is converted to a false failure merely because the queue is full.
        """
        with self._lock:
            self._refresh_intent_states()
            allowed = {str(value) for value in intent_ids or set() if str(value)}
            intents = [
                row
                for row in self.store.acquisition_documents(INTENTS)
                if row.get("state") == "pending"
                and (selection_id is None or row.get("selection_id") == selection_id)
                and (
                    not allowed
                    or str(row.get("_id", row.get("id"))) in allowed
                )
            ]
            intents.sort(key=lambda row: str(row.get("updated_at") or ""))
            while intents:
                capacity = self._download_queue_capacity()
                if capacity <= 0:
                    break
                first = intents[0]
                requires_cookies = bool(first.get("requires_cookies"))
                if requires_cookies and not cookie_text:
                    first.update(
                        state="needs_cookies",
                        error="Chọn lại file cookies để tiếp tục lượt tải.",
                        updated_at=self._now(),
                    )
                    self.store.upsert_acquisition_document(INTENTS, first)
                    intents.pop(0)
                    continue
                quality = str(first.get("quality") or "1080")
                connection_id = str(first.get("connection_id") or "") or None
                chunk = [
                    row
                    for row in intents
                    if str(row.get("quality") or "1080") == quality
                    and bool(row.get("requires_cookies")) == requires_cookies
                    and (str(row.get("connection_id") or "") or None) == connection_id
                ][: min(20, capacity)]
                candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for intent in chunk:
                    candidate_id = str(intent.get("candidate_id") or "")
                    candidate = self.store.acquisition_document(CANDIDATES, candidate_id)
                    if candidate is None:
                        intent.update(
                            state="failed",
                            error="Candidate không còn tồn tại.",
                            updated_at=self._now(),
                        )
                        self.store.upsert_acquisition_document(INTENTS, intent)
                        continue
                    candidates.append((candidate, intent))
                if not candidates:
                    intents = [row for row in intents if row not in chunk]
                    continue
                try:
                    records = self._submit_downloads(
                        [candidate["canonical_url"] for candidate, _ in candidates],
                        quality=quality,
                        cookie_text=cookie_text if requires_cookies else None,
                        connection_id=connection_id,
                        intent_keys=[
                            str(intent.get("_id", intent.get("id")))
                            for _, intent in candidates
                        ],
                    )
                except SubtitleJobQueueFull:
                    # Another admission raced this worker. Leave the intents
                    # pending and let the next scheduler/polling pass retry.
                    break
                except ValueError as exc:
                    message = str(exc)
                    needs_cookies = "cookie" in message.casefold()
                    for _, intent in candidates:
                        intent.update(
                            state="needs_cookies" if needs_cookies else "failed",
                            error=(
                                "Chọn lại file cookies để tiếp tục lượt tải."
                                if needs_cookies
                                else "Không thể xếp job tải video lúc này."
                            ),
                            updated_at=self._now(),
                        )
                        self.store.upsert_acquisition_document(INTENTS, intent)
                    intents = [row for row in intents if row not in chunk]
                    continue
                by_url = {str(row.get("url")): row for row in records}
                for candidate, intent in candidates:
                    job = by_url.get(str(candidate["canonical_url"]))
                    if job is None:
                        intent.update(
                            state="pending",
                            error="Downloader chưa nhận job; sẽ thử lại ở lượt kế tiếp.",
                            updated_at=self._now(),
                        )
                    else:
                        self._attach_download_provenance(job.get("id"), candidate)
                        intent.update(
                            state=job.get("state", "queued"),
                            job_id=job.get("id"),
                            error=job.get("error"),
                            updated_at=self._now(),
                        )
                    self.store.upsert_acquisition_document(INTENTS, intent)
                admitted_ids = {
                    str(row.get("_id", row.get("id"))) for row in chunk
                }
                intents = [
                    row
                    for row in intents
                    if str(row.get("_id", row.get("id"))) not in admitted_ids
                ]

    def select_downloads(
        self,
        candidate_ids: list[str],
        *,
        quality: str = "1080",
        idempotency_key: str | None = None,
        cookie_text: str | None = None,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_feature_enabled()
        if quality not in {"best", "1080", "720", "480"}:
            raise AcquisitionError("Chất lượng video không hợp lệ.")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise AcquisitionError("idempotency_key phải là chuỗi.")
            idempotency_key = idempotency_key.strip() or None
            if idempotency_key is not None and len(idempotency_key) > 256:
                raise AcquisitionError("idempotency_key quá dài.")
        normalized_connection = self._normalize_download_connection(connection_id)
        if cookie_text and normalized_connection:
            raise AcquisitionError(
                "Chọn file cookies hoặc connection profile, không dùng đồng thời."
            )
        unique_ids = list(dict.fromkeys(str(value).strip() for value in candidate_ids if str(value).strip()))
        if not unique_ids or len(unique_ids) > 100:
            raise AcquisitionError("Mỗi selection phải có từ 1 đến 100 candidate.", code="BUDGET_EXCEEDED")
        with self._lock:
            if not self.store.is_available:
                raise AcquisitionError(
                    "Kho dữ liệu chưa sẵn sàng; chưa nhận selection tải.",
                    code="STORAGE_UNAVAILABLE",
                )
            previous = None
            if idempotency_key:
                previous = next(
                    (
                        row for row in self.store.acquisition_documents(SELECTIONS)
                        if row.get("idempotency_key") == idempotency_key
                    ),
                    None,
                )
                if previous and "intent_ids" in previous:
                    return self._selection_view(previous)
            candidates_by_id: dict[str, dict[str, Any]] = {}
            for candidate_id in unique_ids:
                candidate = self.store.acquisition_document(CANDIDATES, candidate_id)
                if candidate is None:
                    raise AcquisitionError(
                        "Một candidate không còn tồn tại.", code="NOT_FOUND"
                    )
                candidates_by_id[candidate_id] = candidate
            if normalized_connection and any(
                self._source_for_url(str(candidate.get("canonical_url") or "")) != "bilibili"
                for candidate in candidates_by_id.values()
            ):
                raise AcquisitionError(
                    "connection_id hiện chỉ hỗ trợ tải Bilibili qua CBCE.",
                    code="UNSUPPORTED_OPERATION",
                )
            selection_id = str(
                (previous or {}).get("_id")
                or (previous or {}).get("id")
                or self._selection_id(idempotency_key)
            )
            selection = {
                "_id": selection_id,
                "idempotency_key": idempotency_key,
                "quality": quality,
                "connection_id": normalized_connection,
                "candidate_ids": unique_ids,
                "state": "queued",
                "created_at": self._now(),
                "updated_at": self._now(),
            }
            reserved = self.store.reserve_acquisition_selection(selection)
            reserved_id = str(
                reserved.get("id") or reserved.get("_id") or selection_id
            )
            if reserved_id != selection_id:
                if "intent_ids" in reserved:
                    return self._selection_view(reserved)
                selection = reserved
            else:
                selection = reserved
            selection_id = reserved_id
            self._refresh_intent_states()
            intent_ids: list[str] = []
            for candidate_id in unique_ids:
                candidate = candidates_by_id[candidate_id]
                if candidate.get("download_available") is False:
                    intent_id = self._download_intent_id(
                        candidate_id, quality, normalized_connection
                    )
                    intent = {
                        "_id": intent_id,
                        "selection_id": selection_id,
                        "candidate_id": candidate_id,
                        "quality": quality,
                        "connection_id": normalized_connection,
                        "state": "failed",
                        "error": str(
                            candidate.get("download_unavailable_reason")
                            or "Provider chỉ trả metadata; chưa có media download contract."
                        )[:500],
                        "requires_cookies": bool(cookie_text),
                        "updated_at": self._now(),
                    }
                    self.store.upsert_acquisition_document(INTENTS, intent)
                    intent_ids.append(intent["_id"])
                    continue
                if candidate.get("media_type") != "video":
                    intent_id = self._download_intent_id(
                        candidate_id, quality, normalized_connection
                    )
                    intent = {
                        "_id": intent_id,
                        "selection_id": selection_id,
                        "candidate_id": candidate_id,
                        "quality": quality,
                        "connection_id": normalized_connection,
                        "state": "failed",
                        "error": "Candidate không phải video có thể tải.",
                        "requires_cookies": bool(cookie_text),
                        "updated_at": self._now(),
                    }
                    self.store.upsert_acquisition_document(INTENTS, intent)
                    intent_ids.append(intent["_id"])
                    continue
                intent_id = self._download_intent_id(
                    candidate_id, quality, normalized_connection
                )
                intent = self.store.acquisition_document(INTENTS, intent_id)
                if intent is None:
                    intent = {
                        "_id": intent_id,
                        "selection_id": selection_id,
                        "candidate_id": candidate_id,
                        "quality": quality,
                        "connection_id": normalized_connection,
                        "state": "pending",
                        "job_id": None,
                        "error": None,
                        "requires_cookies": bool(cookie_text),
                        "updated_at": self._now(),
                    }
                else:
                    intent["selection_id"] = selection_id
                    intent["connection_id"] = normalized_connection
                    if cookie_text:
                        intent["requires_cookies"] = True
                self.store.upsert_acquisition_document(INTENTS, intent)
                intent_ids.append(intent_id)
                if intent.get("job_id") and (
                    intent.get("state") in {"queued", "running"}
                    or (
                        intent.get("state") == "succeeded"
                        and getattr(self.video_downloads, "job_is_published", lambda _id: False)(intent["job_id"])
                    )
                ):
                    self._attach_download_provenance(intent.get("job_id"), candidate)
                    continue
                jobs = self.video_downloads.list()
                existing_job = next(
                    (
                        job
                        for job in jobs
                        if job.get("url") == candidate.get("canonical_url")
                        and job.get("quality") == quality
                        and (str(job.get("connection_id") or "") or None)
                        == normalized_connection
                    ),
                    None,
                )
                if existing_job and existing_job.get("state") in {"queued", "running", "succeeded"}:
                    self._attach_download_provenance(existing_job.get("id"), candidate)
                    intent.update(state=existing_job["state"], job_id=existing_job["id"], error=existing_job.get("error"), updated_at=self._now())
                    self.store.upsert_acquisition_document(INTENTS, intent)
                else:
                    intent.update(
                        state=(
                            "needs_cookies"
                            if intent.get("requires_cookies") and not cookie_text
                            else "pending"
                        ),
                        error=(
                            "Chọn lại file cookies để tiếp tục lượt tải."
                            if intent.get("requires_cookies") and not cookie_text
                            else None
                        ),
                        job_id=None,
                        updated_at=self._now(),
                    )
                    self.store.upsert_acquisition_document(INTENTS, intent)
            selection["intent_ids"] = intent_ids
            selection["state"] = "queued" if intent_ids else "failed"
            saved = self._save_selection(selection)
            self._drain_pending_downloads(
                selection_id=selection_id,
                intent_ids=set(intent_ids),
                cookie_text=cookie_text,
            )
            return self._selection_view(saved)

    def _save_selection(self, selection: dict[str, Any]) -> dict[str, Any]:
        selection["updated_at"] = self._now()
        return self.store.upsert_acquisition_document(SELECTIONS, selection)

    def get_selection(self, selection_id: str) -> dict[str, Any] | None:
        selection = self.store.acquisition_document(SELECTIONS, selection_id)
        if selection:
            self._drain_pending_downloads(selection_id=selection_id)
        return self._selection_view(selection) if selection else None

    def list_selections(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.store.acquisition_documents(SELECTIONS, limit=min(max(limit, 1), 100))
        return [self._selection_view(row) for row in rows]

    def control_selection(
        self,
        selection_id: str,
        action: str,
        *,
        cookie_text: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"pause", "resume", "cancel"}:
            raise AcquisitionError("Thao tác selection không hợp lệ.")
        if action == "resume":
            self._ensure_feature_enabled()
        with self._lock:
            selection = self.store.acquisition_document(SELECTIONS, selection_id)
            if selection is None:
                raise AcquisitionError("Không tìm thấy selection.", code="NOT_FOUND")
            intents = self._selection_intents(selection)
            for intent in intents:
                state = str(intent.get("state") or "unknown")
                job_id = intent.get("job_id")
                job = self.video_downloads.get(job_id) if isinstance(job_id, str) else None
                try:
                    if action == "pause" and state in {"queued", "running"}:
                        pause = getattr(self.video_downloads, "pause", None)
                        if not callable(pause):
                            raise AcquisitionError(
                                "Downloader hiện tại chưa hỗ trợ tạm dừng theo nhóm.",
                                code="UNSUPPORTED_OPERATION",
                            )
                        job = pause(job_id)
                    elif action == "pause" and state in {"pending", "needs_cookies"}:
                        intent.update(
                            state="paused",
                            error=None,
                            updated_at=self._now(),
                        )
                        self.store.upsert_acquisition_document(INTENTS, intent)
                        continue
                    elif action == "cancel" and state in {
                        "queued",
                        "running",
                        "paused",
                        "pending",
                        "needs_cookies",
                        "failed",
                        "missing",
                    }:
                        if isinstance(job_id, str) and state in {"queued", "running"}:
                            job = self.video_downloads.cancel(job_id)
                        intent.update(
                            state="canceled",
                            error=None,
                            updated_at=self._now(),
                        )
                        self.store.upsert_acquisition_document(INTENTS, intent)
                        continue
                    elif action == "resume" and state in {
                        "paused",
                        "pending",
                        "canceled",
                        "failed",
                        "missing",
                        "needs_cookies",
                    }:
                        if not isinstance(job_id, str):
                            intent.update(
                                state=(
                                    "needs_cookies"
                                    if intent.get("requires_cookies") and not cookie_text
                                    else "pending"
                                ),
                                error=(
                                    "Chọn lại file cookies để tiếp tục lượt tải."
                                    if intent.get("requires_cookies") and not cookie_text
                                    else None
                                ),
                                updated_at=self._now(),
                            )
                            self.store.upsert_acquisition_document(INTENTS, intent)
                            continue
                        job = self._retry_download(
                            job_id,
                            cookie_text=cookie_text,
                            connection_id=str(intent.get("connection_id") or "") or None,
                        )
                except AcquisitionError:
                    raise
                except ValueError as exc:
                    message = str(exc)
                    needs_cookies = "cookie" in message.casefold()
                    intent.update(
                        state="needs_cookies" if needs_cookies else "failed",
                        error=(
                            "Chọn lại file cookies để tiếp tục lượt tải."
                            if needs_cookies
                            else "Không thể tiếp tục job tải lúc này."
                        ),
                        updated_at=self._now(),
                    )
                    self.store.upsert_acquisition_document(INTENTS, intent)
                    continue
                except Exception:
                    logger.exception("Could not control acquisition selection %s", selection_id)
                    intent.update(
                        state="failed",
                        error="Không thể điều khiển job tải lúc này.",
                        updated_at=self._now(),
                    )
                    self.store.upsert_acquisition_document(INTENTS, intent)
                    continue
                if job is not None:
                    intent.update(
                        state=job.get("state", state),
                        job_id=job.get("id", job_id),
                        error=job.get("error"),
                        updated_at=self._now(),
                    )
                    self.store.upsert_acquisition_document(INTENTS, intent)
                elif action in {"pause", "resume"}:
                    intent.update(
                        state=(
                            "needs_cookies"
                            if intent.get("requires_cookies") and not cookie_text
                            else "pending"
                        ),
                        error=(
                            "Chọn lại file cookies để tiếp tục lượt tải."
                            if intent.get("requires_cookies") and not cookie_text
                            else None
                        ),
                        updated_at=self._now(),
                    )
                    self.store.upsert_acquisition_document(INTENTS, intent)
            self._drain_pending_downloads(
                selection_id=selection_id,
                cookie_text=cookie_text,
            )
            selection["state"] = self._selection_state(selection, intents)
            self._save_selection(selection)
            return self._selection_view(selection)

    def list_channels(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.store.acquisition_documents(CHANNELS)
        active = [row for row in rows if row.get("enabled", True)]
        return active[: min(max(limit, 1), 100)]

    def update_channel(
        self,
        channel_id: str,
        *,
        label: str | None = None,
        connection_id: str | None | object = _UNSET,
    ) -> dict[str, Any] | None:
        """Update channel presentation or its optional app-owned connection.

        The canonical target remains immutable so its stable channel ID and
        subscription observations are not silently moved to another source.
        Passing ``None`` for ``connection_id`` explicitly clears it; omitting
        the field leaves the existing connection unchanged.
        """
        channel = self.store.acquisition_document(CHANNELS, channel_id)
        if channel is None:
            return None
        if label is not None:
            parsed = urlsplit(str(channel.get("canonical_url") or ""))
            channel["label"] = str(label).strip()[:120] or parsed.netloc
        if connection_id is not _UNSET:
            normalized_connection = self._normalize_channel_connection(
                str(channel.get("source_id") or ""), connection_id
            )
            if normalized_connection is None:
                channel.pop("connection_id", None)
            else:
                channel["connection_id"] = normalized_connection
        channel["updated_at"] = self._now()
        return self.store.upsert_acquisition_document(CHANNELS, channel)

    def delete_channel(self, channel_id: str) -> bool:
        """Stop tracking a channel without deleting runs, candidates or files."""
        with self._lock:
            channel = self.store.acquisition_document(CHANNELS, channel_id)
            if channel is None:
                return False
            if not channel.get("enabled", True):
                return True
            subscription = dict(channel.get("subscription") or {})
            active_run_id = subscription.get("active_run_id")
            if isinstance(active_run_id, str):
                active = self.get_run(active_run_id)
                if active and active.get("state") in ACTIVE_RUN_STATES:
                    self.cancel(active_run_id)
            subscription.update(
                enabled=False,
                active_run_id=None,
                lease_expires_at=None,
                reconcile_run_id=None,
                reconcile_lease_token=None,
                reconcile_lease_expires_at=None,
                next_run_at=None,
            )
            channel["subscription"] = subscription
            channel["enabled"] = False
            channel["deleted_at"] = self._now()
            channel["updated_at"] = self._now()
            self.store.upsert_acquisition_document(CHANNELS, channel)
            return True

    @staticmethod
    def _normalize_channel_connection(
        source_id: str, connection_id: str | None | object
    ) -> str | None:
        if connection_id is None:
            return None
        if not isinstance(connection_id, str):
            raise AcquisitionError("connection_id phải là chuỗi.")
        if not connection_id.strip():
            return None
        try:
            normalized_connection = normalize_account_ref(connection_id)
        except (TypeError, ValueError) as exc:
            raise AcquisitionError("connection_id không hợp lệ.") from exc
        if len(normalized_connection) > 128:
            raise AcquisitionError("connection_id tối đa 128 ký tự.")
        if source_id != "bilibili":
            raise AcquisitionError(
                "connection_id hiện chỉ hỗ trợ kênh Bilibili qua CBCE.",
                code="UNSUPPORTED_OPERATION",
            )
        return normalized_connection

    def save_channel(
        self,
        url: str,
        *,
        label: str = "",
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_feature_enabled()
        try:
            canonical_url = normalize_video_url(url)
        except ValueError as exc:
            raise AcquisitionError("Link kênh hoặc playlist không hợp lệ.") from exc
        source_id = self._source_for_url(canonical_url)
        if source_id is None:
            raise AcquisitionError(
                "Chưa nhận diện được nền tảng của kênh.", code="UNSUPPORTED_OPERATION"
            )
        normalized_connection = self._normalize_channel_connection(source_id, connection_id)
        parsed = urlsplit(canonical_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path.rstrip("/") or "/"
        target_kind = "creator"
        if source_id == "youtube":
            if path == "/watch" and not query.get("list"):
                raise AcquisitionError("Link video không phải là kênh hoặc playlist.")
            target_kind = "playlist" if query.get("list") or path == "/playlist" else "creator"
            if target_kind == "creator" and not any(
                path.startswith(prefix) for prefix in ("/channel/", "/@", "/c/", "/user/")
            ):
                raise AcquisitionError("Link YouTube phải chỉ tới một kênh hoặc playlist.")
        elif source_id == "bilibili":
            from ..crawlers.adapters.bilibili.targets import (
                BilibiliTargetKind,
                parse_bilibili_target,
            )

            try:
                target = parse_bilibili_target(canonical_url)
            except ValueError as exc:
                raise AcquisitionError("Link Bilibili không phải creator hoặc playlist hợp lệ.") from exc
            if target.kind not in {BilibiliTargetKind.CREATOR, BilibiliTargetKind.PLAYLIST}:
                raise AcquisitionError("Link Bilibili phải chỉ tới một creator hoặc playlist.")
            # Collapse favlist/list aliases to the stable medialist URL before
            # deriving the channel identity. The actual playlist operation
            # remains capability-gated until its live provider gate passes.
            canonical_url = target.canonical_url
            target_kind = target.kind.value
        channel_id = hashlib.sha256(
            f"{source_id}\x00{canonical_url}".encode()
        ).hexdigest()[:32]
        existing = self.store.acquisition_document(CHANNELS, channel_id) or {
            "_id": channel_id,
            "created_at": self._now(),
        }
        subscription = dict(existing.get("subscription") or {})
        subscription.setdefault("enabled", False)
        subscription.setdefault("initial_policy", "baseline")
        subscription.setdefault("auto_download", False)
        subscription.setdefault("interval_minutes", 360)
        subscription.setdefault("quality", "1080")
        subscription.setdefault("max_items", 20)
        changes = {
            "source_id": source_id,
            "canonical_url": canonical_url,
            "label": str(label).strip()[:120] or parsed.netloc,
            "target_kind": target_kind,
            "enabled": True,
            "subscription": subscription,
            "updated_at": self._now(),
        }
        existing.pop("deleted_at", None)
        if connection_id is not None:
            changes["connection_id"] = normalized_connection
        existing.update(changes)
        return self.store.upsert_acquisition_document(CHANNELS, existing)

    def update_subscription(
        self,
        channel_id: str,
        *,
        enabled: bool,
        initial_policy: str = "baseline",
        auto_download: bool = False,
        interval_minutes: int = 360,
        quality: str = "1080",
        max_items: int = 20,
    ) -> dict[str, Any] | None:
        if enabled:
            self._ensure_feature_enabled()
        if initial_policy not in {"baseline", "backfill"}:
            raise AcquisitionError("Chính sách lần quét đầu không hợp lệ.")
        if quality not in {"best", "1080", "720", "480"}:
            raise AcquisitionError("Chất lượng tự tải không hợp lệ.")
        if not 30 <= interval_minutes <= 10_080 or not 1 <= max_items <= 100:
            raise AcquisitionError("Hạn mức theo dõi không hợp lệ.", code="BUDGET_EXCEEDED")
        channel = self.store.acquisition_document(CHANNELS, channel_id)
        if channel is None:
            return None
        previous = channel.get("subscription") or {}
        subscription = {
            "enabled": bool(enabled),
            "initial_policy": initial_policy,
            "auto_download": bool(auto_download),
            "interval_minutes": interval_minutes,
            "quality": quality,
            "max_items": max_items,
            "baseline_initialized": bool(previous.get("baseline_initialized", False)),
            "baseline_at": previous.get("baseline_at") or (self._now() if enabled else None),
            "next_run_at": previous.get("next_run_at"),
            "active_run_id": previous.get("active_run_id"),
            "lease_expires_at": previous.get("lease_expires_at"),
            "reconcile_run_id": previous.get("reconcile_run_id"),
            "reconcile_lease_token": previous.get("reconcile_lease_token"),
            "reconcile_lease_expires_at": previous.get("reconcile_lease_expires_at"),
            "last_run_id": previous.get("last_run_id"),
            "last_scanned_at": previous.get("last_scanned_at"),
            "last_status": previous.get("last_status"),
            "last_error": previous.get("last_error"),
            "deferred_auto_download_ids": (
                list(previous.get("deferred_auto_download_ids") or [])
                if auto_download
                else []
            ),
        }
        if not enabled:
            subscription["active_run_id"] = None
            subscription["lease_expires_at"] = None
            subscription["reconcile_run_id"] = None
            subscription["reconcile_lease_token"] = None
            subscription["reconcile_lease_expires_at"] = None
            subscription["next_run_at"] = None
        elif (
            not previous.get("enabled")
            or previous.get("last_status") == "waiting_capability"
        ):
            subscription["next_run_at"] = self._now()
        channel["subscription"] = subscription
        channel["updated_at"] = self._now()
        return self.store.upsert_acquisition_document(CHANNELS, channel)

    def _finalize_subscription(self, subscription_id: str, run_id: str) -> None:
        run = self.store.acquisition_document(RUNS, run_id)
        if run is None or run.get("state") != "completed":
            return
        now = datetime.now(UTC)
        claimed = self.store.claim_acquisition_reconciliation(
            subscription_id,
            run_id=run_id,
            now=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        if claimed is None:
            return
        channel = claimed
        subscription = dict(channel.get("subscription") or {})
        lease_token = subscription["reconcile_lease_token"]
        relations = [
            row for row in self.store.acquisition_documents(RUN_ITEMS)
            if row.get("run_id") == run_id
        ]
        candidate_ids = [str(row["candidate_id"]) for row in relations if row.get("candidate_id")]
        baseline = not bool(subscription.get("baseline_initialized"))
        baseline_at = self._parse_iso(subscription.get("baseline_at"))
        if baseline_at is None:
            # Legacy subscriptions have no trustworthy historical boundary.
            # Establish one now instead of treating unseen history as new.
            baseline_at = self._parse_iso(run.get("created_at")) or datetime.now(UTC)
            subscription["baseline_at"] = baseline_at.isoformat()
        new_ids: list[str] = []
        for candidate_id in candidate_ids[: int(subscription.get("max_items", 20))]:
            observation_id = f"{subscription_id}:{candidate_id}"
            existing = self.store.acquisition_document(SUBSCRIPTION_OBSERVATIONS, observation_id)
            if existing is None or existing.get("classification") == "unknown":
                candidate = self.store.acquisition_document(CANDIDATES, candidate_id) or {}
                published = self._parse_iso(candidate.get("published_at"))
                if baseline:
                    classification = "backfill" if subscription.get("initial_policy") == "backfill" else "baseline"
                elif published is None or published > datetime.now(UTC):
                    classification = "unknown"
                else:
                    classification = "new" if published >= baseline_at else "historical"
                self.store.upsert_acquisition_document(
                    SUBSCRIPTION_OBSERVATIONS,
                    {
                        "_id": observation_id,
                        "subscription_id": subscription_id,
                        "candidate_id": candidate_id,
                        "run_id": run_id,
                        "classification": classification,
                        "observed_at": self._now(),
                    },
                )
                if classification in {"new", "backfill"}:
                    new_ids.append(candidate_id)
            elif existing.get("run_id") == run_id and existing.get("classification") in {"new", "backfill"}:
                # Replay the same durable selection after an interrupted finalizer.
                new_ids.append(candidate_id)
        latest_channel = self.store.acquisition_document(CHANNELS, subscription_id)
        if latest_channel is None:
            return
        latest_subscription = dict(latest_channel.get("subscription") or {})
        lease_expiry = self._parse_iso(latest_subscription.get("reconcile_lease_expires_at"))
        if (
            latest_subscription.get("active_run_id") != run_id
            or latest_subscription.get("reconcile_run_id") != run_id
            or latest_subscription.get("reconcile_lease_token") != lease_token
            or lease_expiry is None
            or lease_expiry <= datetime.now(UTC)
        ):
            return
        subscription = latest_subscription
        deferred_ids = [
            str(value)
            for value in subscription.get("deferred_auto_download_ids") or []
            if str(value).strip()
        ]
        selection_ids = list(dict.fromkeys(new_ids))
        pending_deferred_ids = list(dict.fromkeys(deferred_ids))
        if subscription.get("enabled") and subscription.get("auto_download"):
            if settings.content_bot_acquisition_enabled:
                selection_ids = list(dict.fromkeys((*pending_deferred_ids, *selection_ids)))
                if selection_ids:
                    self.select_downloads(
                        selection_ids,
                        quality=str(subscription.get("quality") or "1080"),
                        idempotency_key=f"subscription:{subscription_id}:{run_id}",
                        connection_id=channel.get("connection_id"),
                    )
                pending_deferred_ids = []
            elif new_ids:
                pending_deferred_ids = list(
                    dict.fromkeys((*pending_deferred_ids, *new_ids))
                )[:100]
        deferred_after_flag = bool(
            not settings.content_bot_acquisition_enabled
            and subscription.get("enabled")
            and subscription.get("auto_download")
            and new_ids
        )
        completion_updates = {
            "baseline_initialized": True,
            "active_run_id": None,
            "lease_expires_at": None,
            "reconcile_run_id": None,
            "reconcile_lease_token": None,
            "reconcile_lease_expires_at": None,
            "last_run_id": run_id,
            "last_scanned_at": self._now(),
            "last_status": "deferred_feature" if deferred_after_flag else "succeeded",
            "last_error": (
                "Auto-download tạm hoãn vì acquisition đang tắt trong rollout."
                if deferred_after_flag
                else None
            ),
        }
        if subscription.get("auto_download"):
            completion_updates["deferred_auto_download_ids"] = pending_deferred_ids
        if subscription.get("enabled"):
            completion_updates["next_run_at"] = (
                self._now()
                if deferred_after_flag
                else (
                    datetime.now(UTC)
                    + timedelta(minutes=int(subscription.get("interval_minutes", 360)))
                ).isoformat()
            )
        self.store.complete_acquisition_reconciliation(
            subscription_id,
            run_id=run_id,
            lease_token=lease_token,
            now=self._now(),
            updates=completion_updates,
        )

    def _subscription_capability(
        self, channel: dict[str, Any]
    ) -> dict[str, Any] | None:
        source_id = str(channel.get("source_id") or "")
        operation = str(channel.get("target_kind") or "creator")
        for item in self.capabilities().get("items", []):
            if item.get("source_id") != source_id:
                continue
            operations = item.get("operations") or {}
            capability = operations.get(operation)
            return capability if isinstance(capability, dict) else None
        return None

    def scheduler_tick(self) -> None:
        """Schedule bounded subscription scans; user action owns enablement."""
        if (
            not settings.content_bot_acquisition_enabled
            or not self._accepting
            or not self.store.is_available
        ):
            return
        if not self._scheduler_lock.acquire(blocking=False):
            return
        try:
            self._drain_pending_downloads()
            self._scheduler_tick_locked()
        finally:
            self._scheduler_lock.release()

    def _scheduler_tick_locked(self) -> None:
        now = datetime.now(UTC)
        for channel in self.list_channels(limit=100):
            subscription = dict(channel.get("subscription") or {})
            if not subscription.get("enabled"):
                continue
            active_run_id = subscription.get("active_run_id")
            if isinstance(active_run_id, str):
                active = self.get_run(active_run_id)
                if active and active.get("state") in ACTIVE_RUN_STATES:
                    if (
                        active.get("state") == "paused"
                        and active.get("stop_reason") == "application_restart"
                    ):
                        subscription.update(
                            active_run_id=None,
                            lease_expires_at=None,
                            reconcile_run_id=None,
                            reconcile_lease_token=None,
                            reconcile_lease_expires_at=None,
                            last_status="catching_up",
                            last_error=(
                                "Lượt theo dõi được xếp lại sau khi ứng dụng khởi động lại."
                            ),
                            next_run_at=now.isoformat(),
                        )
                        channel["subscription"] = subscription
                        channel["updated_at"] = self._now()
                        self.store.upsert_acquisition_document(CHANNELS, channel)
                    continue
                if active and subscription.get("last_run_id") != active_run_id:
                    try:
                        if active.get("state") == "completed":
                            self._finalize_subscription(channel["id"], active_run_id)
                        else:
                            self._fail_subscription(
                                channel["id"],
                                "Lượt theo dõi trước đã dừng trước khi hoàn tất.",
                            )
                    except Exception:
                        logger.exception(
                            "Could not reconcile subscription run %s", active_run_id
                        )
                        self._fail_subscription(
                            channel["id"], "Không thể đồng bộ lượt theo dõi trước."
                        )
                    continue
            capability = self._subscription_capability(channel)
            if not capability or not capability.get("enabled"):
                next_retry = (now + timedelta(minutes=30)).isoformat()
                if (
                    subscription.get("last_status") != "waiting_capability"
                    or self._parse_iso(subscription.get("next_run_at")) is None
                    or self._parse_iso(subscription.get("next_run_at")) <= now
                ):
                    subscription.update(
                        active_run_id=None,
                        lease_expires_at=None,
                        last_status="waiting_capability",
                        last_error=str(
                            (capability or {}).get(
                                "detail",
                                "Operation của nguồn chưa được capability gate.",
                            )
                        )[:500],
                        next_run_at=next_retry,
                    )
                    channel["subscription"] = subscription
                    channel["updated_at"] = self._now()
                    self.store.upsert_acquisition_document(CHANNELS, channel)
                continue
            next_run = self._parse_iso(subscription.get("next_run_at"))
            if next_run is not None and next_run > now:
                continue
            request = {
                "mode": channel.get("target_kind") or "creator",
                "targets": [channel["canonical_url"]],
                "limits": {
                    "max_candidates": min(int(subscription.get("max_items", 20)), 100),
                    "max_pages": 20,
                    "deadline_seconds": 300,
                },
                "filters": {"media_type": "video", "only_not_downloaded": True},
                "subscription_id": channel["id"],
            }
            if channel.get("connection_id"):
                request["connection_id"] = channel["connection_id"]
            run_id = uuid.uuid4().hex
            lease_expires_at = (now + timedelta(minutes=15)).isoformat()
            claimed = self.store.claim_acquisition_channel(
                channel["id"],
                run_id=run_id,
                now=now.isoformat(),
                lease_expires_at=lease_expires_at,
            )
            if claimed is None:
                continue
            subscription = dict(claimed.get("subscription") or subscription)
            channel = claimed
            try:
                run = self.create_run(request, run_id=run_id)
            except AcquisitionError as error:
                subscription.update(
                    active_run_id=None,
                    lease_expires_at=None,
                    last_status="waiting_capacity" if error.code == "QUEUE_FULL" else "failed",
                    last_error=str(error),
                    next_run_at=(now + timedelta(minutes=1 if error.code == "QUEUE_FULL" else 30)).isoformat(),
                )
                channel["subscription"] = subscription
                self.store.upsert_acquisition_document(CHANNELS, channel)
                continue
            except Exception:
                logger.exception("Could not create scheduled acquisition run")
                self._fail_subscription(
                    channel["id"], "Không thể tạo lượt theo dõi theo lịch."
                )
                continue
            # The run can finish before this thread writes the lease (a
            # mocked/very short extractor does exactly that). Do not overwrite
            # the finalizer's next_run_at/baseline state with a stale snapshot.
            latest = self.store.acquisition_document(CHANNELS, channel["id"])
            latest_subscription = dict((latest or {}).get("subscription") or {})
            if latest_subscription.get("last_run_id") != run["id"]:
                subscription["active_run_id"] = run["id"]
                subscription["last_status"] = "queued"
                subscription["last_error"] = None
                channel["subscription"] = subscription
                self.store.upsert_acquisition_document(CHANNELS, channel)

    def start(self) -> None:
        """Mark in-flight metadata runs interrupted; resume stays user-driven."""
        for run in self.store.acquisition_documents(RUNS):
            if run.get("state") in {"queued", "running"}:
                self._save_run(
                    run,
                    state="paused",
                    phase="interrupted",
                    stop_reason="application_restart",
                    error="Ứng dụng đã khởi động lại; bấm Tiếp tục để chạy lại.",
                )

    def stop_accepting(self) -> None:
        self._accepting = False
        with self._lock:
            for event in self._cancel_events.values():
                event.set()

    def shutdown(self, timeout_seconds: float = 10) -> None:
        self.stop_accepting()
        self._executor.shutdown(wait=True, cancel_futures=True)


__all__ = ["AcquisitionError", "AcquisitionManager"]
