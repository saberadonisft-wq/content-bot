from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import shlex
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx

from ..config import settings

DEFAULT_WEB_FEED_URLS = "https://news.google.com/rss/search?q={query}&hl=vi&gl=VN&ceid=VN:vi"
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


def allocate_limits(total: int, buckets: int) -> list[int]:
    if buckets <= 0:
        return []
    base, remainder = divmod(total, buckets)
    return [base + (1 if index < remainder else 0) for index in range(buckets)]


async def get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> httpx.Response:
    """GET with bounded retries for transient transport and HTTP failures."""
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params) if params is not None else await client.get(url)
            status_code = int(getattr(response, "status_code", 200))
            if status_code not in RETRYABLE_HTTP_STATUSES or attempt == attempts - 1:
                response.raise_for_status()
                return response
            retry_after = getattr(response, "headers", {}).get("Retry-After")
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            retry_after = None
        try:
            delay = min(max(float(retry_after), 0), 5) if retry_after else 0.5 * (2**attempt)
        except ValueError:
            delay = 0.5 * (2**attempt)
        await asyncio.sleep(delay)
    raise RuntimeError("HTTP retry loop ended unexpectedly")


@dataclass(frozen=True)
class ConnectorCapabilities:
    global_search: bool
    watchlist_filter: bool = False
    requires_login: bool = False
    interaction_fields: tuple[str, ...] = ()


@dataclass
class ConnectorStatus:
    state: str
    detail: str


@dataclass
class SearchQuery:
    keyword_id: int
    name: str
    include_terms: list[str]
    max_items: int
    progress_callback: Callable[[str, str], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )

    @property
    def search_terms(self) -> list[str]:
        seen: set[str] = set()
        terms: list[str] = []
        for term in (self.name, *self.include_terms):
            cleaned = term.strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                terms.append(cleaned)
        return terms


def login_progress_from_stderr(line: str) -> tuple[str, str] | None:
    """Recognize login confirmation without exposing cookies or QR payloads."""
    stripped = line.strip()
    if stripped.startswith("CONTENT_BOT_PROGRESS "):
        try:
            payload = json.loads(stripped.removeprefix("CONTENT_BOT_PROGRESS "))
        except json.JSONDecodeError:
            return None
        status = payload.get("status")
        if status in {"authenticated", "retrying"}:
            return str(status), str(payload.get("message") or "Login confirmed")
        return None

    lowered = stripped.casefold()
    markers = (
        "login successful",
        "login status confirmed",
        "login state result: true",
        "use cache login state",
        "login state verified",
        "ping zhihu successfully",
    )
    if any(marker in lowered for marker in markers):
        return "authenticated", "Login confirmed by MediaCrawler"
    return None


@dataclass
class RawContentItem:
    external_id: str
    canonical_url: str
    title: str
    body_snippet: str = ""
    author: str = ""
    hashtags: list[str] = field(default_factory=list)
    locale: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, int] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)


class SourceConnector(abc.ABC):
    source_id: str
    label: str
    group: str
    capabilities: ConnectorCapabilities

    @abc.abstractmethod
    async def healthcheck(self) -> ConnectorStatus:
        raise NotImplementedError

    @abc.abstractmethod
    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        raise NotImplementedError


class UnconfiguredConnector(SourceConnector):
    def __init__(self, source_id: str, label: str, group: str, detail: str, capabilities: ConnectorCapabilities):
        self.source_id = source_id
        self.label = label
        self.group = group
        self.detail = detail
        self.capabilities = capabilities

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("not_configured", self.detail)

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        if False:
            yield RawContentItem("", "", "")
        return


class YouTubeConnector(SourceConnector):
    source_id = "youtube"
    label = "YouTube"
    group = "Official API"
    capabilities = ConnectorCapabilities(True, interaction_fields=("view_count", "like_count", "comment_count"))

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.youtube_api_key:
            return ConnectorStatus("not_configured", "Add YOUTUBE_API_KEY in backend/.env to enable official search.")
        return ConnectorStatus("ready", "Official YouTube Data API is configured.")

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        if not settings.youtube_api_key:
            return
        page_token = (checkpoint or {}).get("page_token")
        yielded = 0
        async with httpx.AsyncClient(base_url="https://www.googleapis.com/youtube/v3", timeout=30) as client:
            while yielded < query.max_items:
                response = await get_with_retries(
                    client,
                    "/search",
                    params={
                        "part": "snippet",
                        "type": "video",
                        "q": "|".join(query.search_terms),
                        "maxResults": min(50, query.max_items - yielded),
                        "order": "date",
                        "regionCode": settings.youtube_region_code,
                        "relevanceLanguage": settings.youtube_relevance_language,
                        "publishedAfter": (datetime.now(UTC) - timedelta(days=90)).isoformat().replace("+00:00", "Z"),
                        "pageToken": page_token,
                        "key": settings.youtube_api_key,
                    },
                )
                payload = response.json()
                rows = payload.get("items", [])
                video_ids = [row.get("id", {}).get("videoId") for row in rows if row.get("id", {}).get("videoId")]
                if not video_ids:
                    break
                detail_response = await get_with_retries(
                    client,
                    "/videos",
                    params={"part": "snippet,statistics", "id": ",".join(video_ids), "key": settings.youtube_api_key},
                )
                details = {row["id"]: row for row in detail_response.json().get("items", [])}
                for video_id in video_ids:
                    row = details.get(video_id, {})
                    snippet = row.get("snippet", {})
                    statistics = row.get("statistics", {})
                    published = snippet.get("publishedAt")
                    published_at = datetime.fromisoformat(published) if published else None
                    yield RawContentItem(
                        external_id=video_id,
                        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
                        title=snippet.get("title", ""),
                        body_snippet=snippet.get("description", "")[:4000],
                        author=snippet.get("channelTitle", ""),
                        hashtags=[tag for tag in snippet.get("tags", []) if tag.startswith("#")],
                        locale=snippet.get("defaultLanguage"),
                        published_at=published_at,
                        metrics={
                            "view_count": int(statistics.get("viewCount", 0)),
                            "like_count": int(statistics.get("likeCount", 0)),
                            "comment_count": int(statistics.get("commentCount", 0)),
                        },
                        raw_payload=row,
                    )
                    yielded += 1
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
                await asyncio.sleep(0.2)


class JsonlCommandConnector(SourceConnector):
    """Runs an explicit local adapter; stdout must contain newline-delimited JSON items.

    This keeps MediaCrawler/browser automation out of the API process and never
    tries to create a login session or evade a platform challenge automatically.
    """

    def __init__(self, source_id: str, label: str, detail: str, capabilities: ConnectorCapabilities, platform: str):
        self.source_id = source_id
        self.label = label
        self.group = "MediaCrawler bridge"
        self.detail = detail
        self.capabilities = capabilities
        self.platform = platform

    async def healthcheck(self) -> ConnectorStatus:
        if settings.mediacrawler_command:
            return ConnectorStatus("ready", "Custom local JSONL bridge configured. Browser login remains user-visible.")
        project_root = Path(__file__).resolve().parents[3]
        adapter = project_root / "backend" / "scripts" / "mediacrawler_adapter.py"
        runtime = project_root / "vendor" / "mediacrawler" / ".venv" / "Scripts" / "python.exe"
        ready_marker = project_root / "data" / "mediacrawler-ready"
        if not adapter.exists() or not (project_root / "vendor" / "mediacrawler" / "main.py").exists():
            return ConnectorStatus("not_configured", "MediaCrawler submodule is missing. Run git submodule update --init --recursive.")
        if not runtime.exists() or not ready_marker.exists():
            return ConnectorStatus("setup_required", "Run .\\scripts\\setup-mediacrawler.ps1 once, then restart Content Bot.")
        coccoc_path = Path(settings.content_bot_coccoc_executable_path).expanduser()
        if not coccoc_path.is_file():
            return ConnectorStatus(
                "setup_required",
                f"Cốc Cốc was not found at {coccoc_path}. Set CONTENT_BOT_COCCOC_EXECUTABLE_PATH in backend/.env.",
            )
        return ConnectorStatus("ready", "Direct MediaCrawler adapter is installed. A visible browser opens when login is required.")

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
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
                target_closed = "TargetClosedError" in error_text or "target page, context or browser has been closed" in error_text.casefold()
                if attempt or yielded_any or not target_closed:
                    raise
                if query.progress_callback:
                    try:
                        await query.progress_callback("retrying", "Cốc Cốc page was closed; reopening the browser session")
                    except Exception:
                        logger.debug("Progress callback failed while retrying browser session", exc_info=True)
                await asyncio.sleep(1)

    async def _search_once(self, query: SearchQuery) -> AsyncIterator[RawContentItem]:
        project_root = Path(__file__).resolve().parents[3]
        substitutions = {
            "source": self.platform,
            "keywords": ",".join(query.include_terms),
            "max_items": str(query.max_items),
            "profile_dir": str(settings.mediacrawler_profile_dir),
        }
        if settings.mediacrawler_command:
            command = [part.format(**substitutions) for part in shlex.split(settings.mediacrawler_command, posix=False)]
        else:
            command = [sys.executable, str(project_root / "backend" / "scripts" / "mediacrawler_adapter.py")]
        command.extend(["--source", self.platform, "--keywords", ",".join(query.include_terms), "--max-items", str(query.max_items), "--profile-dir", str(settings.mediacrawler_profile_dir)])
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
            async with asyncio.timeout(settings.mediacrawler_timeout_seconds):
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
                            published_at=datetime.fromisoformat(published) if published else None,
                            metrics={key: int(value or 0) for key, value in payload.get("metrics", {}).items()},
                            raw_payload=payload,
                        )
                        yielded += 1
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                await process.wait()
        except TimeoutError as exc:
            raise RuntimeError("MediaCrawler timed out while waiting for login or source results.") from exc
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
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


class FeedConnector(SourceConnector):
    """Small allowlisted RSS/Atom connector for public game sites and Steam feeds."""

    def __init__(self, source_id: str, label: str, detail: str, url_templates: str):
        self.source_id = source_id
        self.label = label
        self.group = "Public web"
        self.detail = detail
        self.url_templates = [url.strip() for url in url_templates.split(",") if url.strip()]
        self.capabilities = ConnectorCapabilities(True)

    async def healthcheck(self) -> ConnectorStatus:
        if not self.url_templates:
            return ConnectorStatus("not_configured", self.detail)
        return ConnectorStatus("ready", f"{len(self.url_templates)} allowlisted public feed(s) configured.")

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        if not self.url_templates:
            return
        seen: set[str] = set()
        yielded = 0
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "ContentBot/0.1 (local research tool)"}) as client:
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            for search_term, term_limit in zip(query.search_terms, term_limits, strict=True):
                term_yielded = 0
                if term_limit == 0:
                    continue
                for template in self.url_templates:
                    response = await get_with_retries(client, template.format(query=quote_plus(search_term)))
                    try:
                        root = ElementTree.fromstring(response.content)
                    except ElementTree.ParseError as exc:
                        raise RuntimeError(f"Invalid feed XML: {template}") from exc
                    for node in root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry"):
                        if term_yielded >= term_limit:
                            break
                        title = self._text(node, "title")
                        link = self._text(node, "link") or next((entry.get("href", "") for entry in node.findall("{http://www.w3.org/2005/Atom}link") if entry.get("href")), "")
                        external_id = node.findtext("guid") or node.findtext("{http://www.w3.org/2005/Atom}id") or link
                        if not external_id or external_id in seen:
                            continue
                        seen.add(external_id)
                        published = self._text(node, "pubDate") or self._text(node, "published") or self._text(node, "updated")
                        try:
                            published_at = parsedate_to_datetime(published) if published and "," in published else datetime.fromisoformat(published) if published else None
                        except (TypeError, ValueError):
                            published_at = None
                        yield RawContentItem(external_id=external_id, canonical_url=link or external_id, title=title, body_snippet=self._text(node, "description") or self._text(node, "summary"), author=self._text(node, "author"), published_at=published_at, raw_payload={"feed": template, "search_term": search_term})
                        yielded += 1
                        term_yielded += 1
                    if term_yielded >= term_limit:
                        break

    @staticmethod
    def _text(node: ElementTree.Element, tag: str) -> str:
        direct = node.findtext(tag)
        if direct:
            return direct.strip()
        namespaced = node.findtext(f"{{http://www.w3.org/2005/Atom}}{tag}")
        return namespaced.strip() if namespaced else ""


class SteamReviewsConnector(SourceConnector):
    source_id = "steam"
    label = "Steam reviews"
    group = "Public game community"
    capabilities = ConnectorCapabilities(True, interaction_fields=("like_count", "comment_count"))

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "Public Steam game search and recent user reviews; no API key required.")

    async def search(self, query: SearchQuery, checkpoint: dict[str, Any] | None = None) -> AsyncIterator[RawContentItem]:
        yielded = 0
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "ContentBot/0.1 (local non-commercial game research)"},
        ) as client:
            games: list[dict[str, Any]] = []
            seen_apps: set[str] = set()
            game_limits = allocate_limits(3, len(query.search_terms))
            for search_term, game_limit in zip(query.search_terms, game_limits, strict=True):
                term_games = 0
                if game_limit == 0:
                    continue
                search_response = await get_with_retries(
                    client,
                    "https://store.steampowered.com/api/storesearch/",
                    params={"term": search_term, "l": "english", "cc": "VN"},
                )
                for game in search_response.json().get("items", []):
                    app_id = str(game.get("id", ""))
                    if app_id and app_id not in seen_apps:
                        seen_apps.add(app_id)
                        games.append(game)
                        term_games += 1
                    if term_games >= game_limit:
                        break
            review_limits = allocate_limits(query.max_items, len(games))
            for game, review_limit in zip(games, review_limits, strict=True):
                game_yielded = 0
                if review_limit == 0:
                    continue
                app_id = str(game.get("id", ""))
                game_name = str(game.get("name", query.name))
                if not app_id:
                    continue
                cursor = "*"
                while game_yielded < review_limit:
                    response = await get_with_retries(
                        client,
                        f"https://store.steampowered.com/appreviews/{app_id}",
                        params={
                            "json": 1,
                            "filter": "recent",
                            "language": "all",
                            "purchase_type": "all",
                            "num_per_page": min(100, review_limit - game_yielded),
                            "cursor": cursor,
                        },
                    )
                    payload = response.json()
                    reviews = payload.get("reviews", [])
                    if payload.get("success") != 1 or not reviews:
                        break
                    for review in reviews:
                        if game_yielded >= review_limit:
                            break
                        review_id = str(review.get("recommendationid", ""))
                        if not review_id:
                            continue
                        voted_up = bool(review.get("voted_up"))
                        sanitized = {key: value for key, value in review.items() if key != "author"}
                        timestamp = int(review.get("timestamp_created", 0) or 0)
                        yield RawContentItem(
                            external_id=f"{app_id}:{review_id}",
                            canonical_url=f"https://steamcommunity.com/app/{app_id}/reviews/",
                            title=f"{'Recommended' if voted_up else 'Not recommended'} — {game_name}",
                            body_snippet=str(review.get("review", ""))[:4000],
                            author="Steam reviewer",
                            locale=str(review.get("language") or "") or None,
                            published_at=datetime.fromtimestamp(timestamp, tz=UTC) if timestamp else None,
                            metrics={
                                "like_count": int(review.get("votes_up", 0) or 0),
                                "comment_count": int(review.get("comment_count", 0) or 0),
                            },
                            raw_payload={"app_id": app_id, "game_name": game_name, **sanitized},
                        )
                        yielded += 1
                        game_yielded += 1
                    next_cursor = str(payload.get("cursor") or "")
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
                    await asyncio.sleep(0.2)


class BlueskyConnector(SourceConnector):
    source_id = "bluesky"
    label = "Bluesky"
    group = "Public social API"
    capabilities = ConnectorCapabilities(
        True,
        interaction_fields=("like_count", "comment_count", "share_count"),
    )

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "Public Bluesky keyword search; no account or API key required.")

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        yielded = 0
        seen_posts: set[str] = set()
        async with httpx.AsyncClient(
            base_url="https://api.bsky.app",
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        ) as client:
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            for term_index, (search_term, term_limit) in enumerate(
                zip(query.search_terms, term_limits, strict=True)
            ):
                term_yielded = 0
                if term_limit == 0:
                    continue
                cursor = str((checkpoint or {}).get("cursor") or "") if term_index == 0 else ""
                while term_yielded < term_limit:
                    params: dict[str, Any] = {
                        "q": search_term,
                        "sort": "latest",
                        "limit": min(50, term_limit - term_yielded),
                    }
                    if cursor:
                        params["cursor"] = cursor
                    try:
                        response = await get_with_retries(client, "/xrpc/app.bsky.feed.searchPosts", params=params)
                        payload = response.json()
                    except (httpx.HTTPStatusError, httpx.TransportError, RuntimeError):
                        # Gracefully stop paginating on rate limits or 403 blocks instead of failing the job
                        break
                    posts = payload.get("posts", [])
                    if not posts:
                        break
                    for post in posts:
                        if term_yielded >= term_limit:
                            break
                        uri = str(post.get("uri") or "")
                        cid = str(post.get("cid") or "")
                        record = post.get("record") or {}
                        author = post.get("author") or {}
                        handle = str(author.get("handle") or "")
                        record_key = uri.rsplit("/", 1)[-1] if "/" in uri else ""
                        if not cid or cid in seen_posts or not handle or not record_key:
                            continue
                        seen_posts.add(cid)
                        body = str(record.get("text") or "")[:4000]
                        created_at = self._parse_datetime(record.get("createdAt") or post.get("indexedAt"))
                        tags = self._extract_tags(record)
                        yield RawContentItem(
                            external_id=cid,
                            canonical_url=f"https://bsky.app/profile/{handle}/post/{record_key}",
                            title=next((line.strip() for line in body.splitlines() if line.strip()), body)[:180],
                            body_snippet=body,
                            author=handle,
                            hashtags=tags,
                            locale=next(iter(record.get("langs") or []), None),
                            published_at=created_at,
                            metrics={
                                "like_count": int(post.get("likeCount", 0) or 0),
                                "comment_count": int(post.get("replyCount", 0) or 0),
                                "share_count": int(post.get("repostCount", 0) or 0)
                                + int(post.get("quoteCount", 0) or 0),
                            },
                            raw_payload={
                                "cid": cid,
                                "text": body,
                                "created_at": record.get("createdAt"),
                                "indexed_at": post.get("indexedAt"),
                                "langs": record.get("langs") or [],
                                "tags": tags,
                                "search_term": search_term,
                            },
                        )
                        yielded += 1
                        term_yielded += 1
                    next_cursor = str(payload.get("cursor") or "")
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
                    await asyncio.sleep(0.5)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value)) if value else None
        except ValueError:
            return None

    @staticmethod
    def _extract_tags(record: dict[str, Any]) -> list[str]:
        tags: list[str] = []
        for facet in record.get("facets") or []:
            for feature in facet.get("features") or []:
                if feature.get("$type") == "app.bsky.richtext.facet#tag" and feature.get("tag"):
                    tag = f"#{feature['tag']}"
                    if tag not in tags:
                        tags.append(tag)
        return tags


class RedditConnector(SourceConnector):
    source_id = "reddit"
    label = "Reddit"
    group = "Public community"
    capabilities = ConnectorCapabilities(
        True,
        interaction_fields=("like_count", "comment_count"),
    )

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            return ConnectorStatus(
                "not_configured",
                "Add REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET for official Reddit OAuth search.",
            )
        return ConnectorStatus("ready", "Official Reddit OAuth keyword search is configured.")

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        """Search public Reddit submissions through application-only OAuth."""
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            return
        yielded = 0
        seen_posts: set[str] = set()
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": settings.reddit_user_agent},
        ) as client:
            token_response = await client.post(
                "https://api.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                auth=httpx.BasicAuth(settings.reddit_client_id, settings.reddit_client_secret),
            )
            token_response.raise_for_status()
            access_token = str(token_response.json().get("access_token") or "")
            if not access_token:
                raise RuntimeError("Reddit OAuth response did not include an access token")
            client.headers["Authorization"] = f"Bearer {access_token}"
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            for term_index, (search_term, term_limit) in enumerate(
                zip(query.search_terms, term_limits, strict=True)
            ):
                term_yielded = 0
                if term_limit == 0:
                    continue
                after = str((checkpoint or {}).get("after") or "") if term_index == 0 else ""
                while term_yielded < term_limit:
                    params: dict[str, Any] = {
                        "q": search_term,
                        "sort": "new",
                        "limit": min(100, term_limit - term_yielded),
                        "raw_json": 1,
                        "type": "link",
                    }
                    if after:
                        params["after"] = after
                    response = await get_with_retries(
                        client, "https://oauth.reddit.com/search", params=params
                    )
                    listing = response.json().get("data") or {}
                    children = listing.get("children") or []
                    if not children:
                        break
                    for child in children:
                        post = child.get("data") or {}
                        post_id = str(post.get("id") or "")
                        permalink = str(post.get("permalink") or "")
                        if not post_id or post_id in seen_posts or not permalink:
                            continue
                        seen_posts.add(post_id)
                        created_at = self._parse_timestamp(post.get("created_utc"))
                        flair = str(post.get("link_flair_text") or "").strip()
                        yield RawContentItem(
                            external_id=post_id,
                            canonical_url=f"https://www.reddit.com{permalink}",
                            title=str(post.get("title") or "")[:180],
                            body_snippet=str(post.get("selftext") or "")[:4000],
                            author=str(post.get("author") or "[deleted]"),
                            hashtags=[f"#{flair}"] if flair else [],
                            published_at=created_at,
                            metrics={
                                "like_count": int(post.get("score", 0) or 0),
                                "comment_count": int(post.get("num_comments", 0) or 0),
                            },
                            raw_payload={
                                "post_id": post_id,
                                "subreddit": str(post.get("subreddit") or ""),
                                "search_term": search_term,
                                "is_self": bool(post.get("is_self")),
                                "outbound_url": str(post.get("url") or ""),
                                "flair": flair,
                            },
                        )
                        yielded += 1
                        term_yielded += 1
                    next_after = str(listing.get("after") or "")
                    if not next_after or next_after == after:
                        break
                    after = next_after
                    await asyncio.sleep(0.5)

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        try:
            return datetime.fromtimestamp(float(value), tz=UTC) if value is not None else None
        except (TypeError, ValueError, OSError):
            return None


class XConnector(SourceConnector):
    source_id = "x"
    label = "X"
    group = "Official social API"
    capabilities = ConnectorCapabilities(
        True,
        interaction_fields=("like_count", "comment_count", "share_count", "view_count"),
    )

    async def healthcheck(self) -> ConnectorStatus:
        if not settings.x_bearer_token:
            return ConnectorStatus(
                "not_configured",
                "Add X_BEARER_TOKEN with Recent Search access in backend/.env.",
            )
        return ConnectorStatus("ready", "Official X Recent Search API is configured.")

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        if not settings.x_bearer_token:
            return
        yielded = 0
        seen_posts: set[str] = set()
        async with httpx.AsyncClient(
            base_url="https://api.x.com",
            timeout=30,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
        ) as client:
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            for term_index, (search_term, term_limit) in enumerate(
                zip(query.search_terms, term_limits, strict=True)
            ):
                term_yielded = 0
                if term_limit == 0:
                    continue
                pagination_token = (
                    str((checkpoint or {}).get("pagination_token") or "") if term_index == 0 else ""
                )
                while term_yielded < term_limit:
                    params: dict[str, Any] = {
                        "query": search_term,
                        "max_results": max(10, min(100, term_limit - term_yielded)),
                        "tweet.fields": "author_id,created_at,lang,public_metrics",
                        "expansions": "author_id",
                        "user.fields": "username",
                    }
                    if pagination_token:
                        params["pagination_token"] = pagination_token
                    response = await get_with_retries(
                        client, "/2/tweets/search/recent", params=params
                    )
                    payload = response.json()
                    users = {
                        str(user.get("id")): user
                        for user in (payload.get("includes") or {}).get("users", [])
                    }
                    posts = payload.get("data") or []
                    if not posts:
                        break
                    for post in posts:
                        if term_yielded >= term_limit:
                            break
                        post_id = str(post.get("id") or "")
                        if not post_id or post_id in seen_posts:
                            continue
                        seen_posts.add(post_id)
                        body = str(post.get("text") or "")[:4000]
                        author_id = str(post.get("author_id") or "")
                        username = str((users.get(author_id) or {}).get("username") or author_id)
                        metrics = post.get("public_metrics") or {}
                        yield RawContentItem(
                            external_id=post_id,
                            canonical_url=f"https://x.com/{username}/status/{post_id}",
                            title=next(
                                (line.strip() for line in body.splitlines() if line.strip()), body
                            )[:180],
                            body_snippet=body,
                            author=username,
                            hashtags=self._extract_hashtags(body),
                            locale=post.get("lang"),
                            published_at=self._parse_datetime(post.get("created_at")),
                            metrics={
                                "like_count": int(metrics.get("like_count", 0) or 0),
                                "comment_count": int(metrics.get("reply_count", 0) or 0),
                                "share_count": int(metrics.get("retweet_count", 0) or 0)
                                + int(metrics.get("quote_count", 0) or 0),
                                "view_count": int(metrics.get("impression_count", 0) or 0),
                            },
                            raw_payload={
                                "post_id": post_id,
                                "author_id": author_id,
                                "search_term": search_term,
                            },
                        )
                        yielded += 1
                        term_yielded += 1
                    next_token = str((payload.get("meta") or {}).get("next_token") or "")
                    if not next_token or next_token == pagination_token:
                        break
                    pagination_token = next_token
                    await asyncio.sleep(0.2)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value)) if value else None
        except ValueError:
            return None

    @staticmethod
    def _extract_hashtags(text: str) -> list[str]:
        return [word.rstrip(".,:;!?)]}") for word in text.split() if word.startswith("#")]


# ---------------------------------------------------------------------------
# Mastodon connector
# ---------------------------------------------------------------------------
# Queries public hashtag timelines across several game-focused Mastodon
# instances.  No API key or account is required; the /api/v1/timelines/tag
# endpoint is public on every instance that allows public preview.
#
# Strategy: each search term is normalised to a hashtag (spaces and hyphens
# removed), then fetched from a fixed list of instances.  Results from all
# instances are deduplicated by canonical URL before yielding.
# ---------------------------------------------------------------------------

_MASTODON_INSTANCES = [
    "mastodon.social",        # largest general instance
    "mastodon.gamedev.place", # game-dev focused
    "dice.camp",              # tabletop / gaming community
]


class MastodonConnector(SourceConnector):
    source_id = "mastodon"
    label = "Mastodon"
    group = "Public social API"
    capabilities = ConnectorCapabilities(
        True,
        interaction_fields=("like_count", "comment_count", "share_count"),
    )

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus(
            "ready",
            "Public Mastodon hashtag search across multiple instances; no API key required.",
        )

    async def search(
        self,
        query: SearchQuery,
        checkpoint: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawContentItem]:
        seen_urls: set[str] = set()
        yielded = 0
        term_limits = allocate_limits(query.max_items, len(query.search_terms))

        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "ContentBot/0.1 (local non-commercial game research)"},
        ) as client:
            for search_term, term_limit in zip(query.search_terms, term_limits, strict=True):
                if term_limit == 0:
                    continue
                # Normalise the term to a valid Mastodon hashtag:
                # lowercase, remove spaces/hyphens/special chars.
                hashtag = search_term.lower()
                for ch in " -_.:,'\"!?":
                    hashtag = hashtag.replace(ch, "")
                if not hashtag:
                    continue

                instance_limit = max(1, term_limit // len(_MASTODON_INSTANCES) + 1)
                term_yielded = 0

                for instance in _MASTODON_INSTANCES:
                    if term_yielded >= term_limit:
                        break
                    try:
                        response = await get_with_retries(
                            client,
                            f"https://{instance}/api/v1/timelines/tag/{quote_plus(hashtag)}",
                            params={
                                "limit": min(40, instance_limit),
                                "remote": "true",
                            },
                        )
                    except Exception:
                        # Skip an unreachable instance rather than failing the whole run.
                        logger.debug("Mastodon instance request failed", exc_info=True)
                        continue

                    posts = response.json()
                    if not isinstance(posts, list):
                        continue

                    for post in posts:
                        if term_yielded >= term_limit:
                            break
                        post_id = str(post.get("id") or "")
                        url = str(post.get("url") or "")
                        if not post_id or not url or url in seen_urls:
                            continue
                        seen_urls.add(url)

                        raw_content = str(post.get("content") or "")
                        # Strip HTML tags from Mastodon's HTML content field.
                        plain_text = self._strip_html(raw_content)[:4000]
                        account = post.get("account") or {}
                        author = str(account.get("acct") or account.get("username") or "")
                        created_at_str = post.get("created_at")
                        created_at = self._parse_datetime(created_at_str)

                        # Extract hashtags from the post's tags list.
                        tags = [
                            f"#{t['name']}"
                            for t in (post.get("tags") or [])
                            if t.get("name")
                        ]

                        # Infer locale from the declared language field (Mastodon 4.x+)
                        locale = post.get("language") or None

                        # Use first non-empty line as title (same pattern as Bluesky/X).
                        title = next(
                            (line.strip() for line in plain_text.splitlines() if line.strip()),
                            plain_text,
                        )[:180]

                        yield RawContentItem(
                            external_id=f"{instance}:{post_id}",
                            canonical_url=url,
                            title=title,
                            body_snippet=plain_text,
                            author=author,
                            hashtags=tags,
                            locale=locale,
                            published_at=created_at,
                            metrics={
                                "like_count": int(post.get("favourites_count") or 0),
                                "comment_count": int(post.get("replies_count") or 0),
                                "share_count": int(post.get("reblogs_count") or 0),
                            },
                            raw_payload={
                                "post_id": post_id,
                                "instance": instance,
                                "hashtag": hashtag,
                                "search_term": search_term,
                            },
                        )
                        yielded += 1
                        term_yielded += 1

                    await asyncio.sleep(0.3)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value)) if value else None
        except ValueError:
            return None

    @staticmethod
    def _strip_html(html: str) -> str:
        """Remove HTML tags and decode common entities from Mastodon content."""
        import re
        # Replace block-level tags with newlines to preserve paragraph breaks.
        text = re.sub(r"<br\s*/?>|</p>", "\n", html, flags=re.IGNORECASE)
        # Strip all remaining tags.
        text = re.sub(r"<[^>]+>", "", text)
        # Decode common HTML entities.
        for entity, char in (
            ("&amp;", "&"),
            ("&lt;", "<"),
            ("&gt;", ">"),
            ("&quot;", '"'),
            ("&#39;", "'"),
            ("&apos;", "'"),
            ("&nbsp;", " "),
        ):
            text = text.replace(entity, char)
        # Collapse multiple blank lines.
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def default_connectors() -> dict[str, SourceConnector]:
    connectors: list[SourceConnector] = [
        YouTubeConnector(),
        FeedConnector(
            "web",
            "Game news",
            "Public keyword news feed is available by default; override it with WEB_FEED_URLS.",
            settings.web_feed_urls or DEFAULT_WEB_FEED_URLS,
        ),
        SteamReviewsConnector(),
        BlueskyConnector(),
        MastodonConnector(),
        RedditConnector()
        if settings.reddit_client_id and settings.reddit_client_secret
        else UnconfiguredConnector(
            "reddit",
            "Reddit",
            "Official API",
            "Add REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET for official Reddit OAuth search.",
            RedditConnector.capabilities,
        ),
        XConnector()
        if settings.x_bearer_token
        else UnconfiguredConnector(
            "x",
            "X",
            "Official social API",
            "Add X_BEARER_TOKEN with Recent Search access in backend/.env.",
            XConnector.capabilities,
        ),
    ]
    media_sources = [
        ("xhs", "Xiaohongshu", "xhs", ("like_count", "comment_count", "favorite_count")),
        ("douyin", "Douyin", "dy", ("like_count", "comment_count", "share_count", "view_count")),
        ("kuaishou", "Kuaishou", "ks", ("like_count", "comment_count", "share_count", "view_count")),
        ("bilibili", "Bilibili", "bili", ("like_count", "comment_count", "favorite_count", "view_count")),
        ("weibo", "Weibo", "wb", ("like_count", "comment_count", "share_count")),
        ("tieba", "Baidu Tieba", "tieba", ("comment_count",)),
        ("zhihu", "Zhihu", "zhihu", ("like_count", "comment_count", "favorite_count")),
    ]
    connectors.extend(
        JsonlCommandConnector(
            source_id, label,
            "Set MEDIACRAWLER_COMMAND to an explicit local adapter after you have logged in visibly.",
            ConnectorCapabilities(True, requires_login=True, interaction_fields=fields), platform,
        )
        for source_id, label, platform, fields in media_sources
    )
    connectors.extend(
        [
            UnconfiguredConnector("tiktok", "TikTok", "Hybrid", "Configure an approved API client or opt in to visible browser login.", ConnectorCapabilities(True, requires_login=True, interaction_fields=("like_count", "comment_count", "share_count", "view_count"))),
            UnconfiguredConnector("facebook", "Facebook", "Hybrid", "Configure approved Page access or opt in to visible browser login for public content.", ConnectorCapabilities(False, watchlist_filter=True, requires_login=True, interaction_fields=("reaction_count", "comment_count", "share_count"))),
            UnconfiguredConnector("instagram", "Instagram", "Hybrid", "Configure approved account/hashtag access or opt in to visible browser login.", ConnectorCapabilities(False, watchlist_filter=True, requires_login=True, interaction_fields=("like_count", "comment_count"))),
        ]
    )
    return {connector.source_id: connector for connector in connectors}
