"""SSRF-safe, budgeted and atomic downloader for crawler media artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .contracts import CancellationToken
from .errors import CrawlerErrorCode, CrawlerFailure

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
}
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class MediaDownloadPolicy:
    source_id: str
    allowed_hosts: tuple[str, ...]
    allowed_content_types: tuple[str, ...] = ("image/", "video/", "audio/")
    max_files: int = 20
    max_total_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 50 * 1024 * 1024
    max_redirects: int = 3
    timeout_seconds: float = 30

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.source_id):
            raise ValueError("Media source ID is invalid")
        hosts = tuple(dict.fromkeys(_host(root) for root in self.allowed_hosts))
        if not hosts:
            raise ValueError("Media host allowlist cannot be empty")
        prefixes = tuple(dict.fromkeys(str(item).strip().casefold() for item in self.allowed_content_types))
        if not prefixes or any(not item.endswith("/") for item in prefixes):
            raise ValueError("Media content-type prefixes are invalid")
        positive = (self.max_files, self.max_total_bytes, self.max_file_bytes)
        if any(value <= 0 for value in positive):
            raise ValueError("Media budgets must be positive")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("Media per-file budget cannot exceed total bytes")
        if not 0 <= self.max_redirects <= 10 or not 1 <= self.timeout_seconds <= 300:
            raise ValueError("Media redirect or timeout policy is invalid")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_content_types", prefixes)


@dataclass(frozen=True, slots=True)
class MediaArtifact:
    source_id: str
    request_id: str
    path: Path
    byte_count: int
    sha256: str
    content_type: str
    source_url: str


class BoundedMediaDownloader:
    def __init__(
        self,
        root: Path,
        policy: MediaDownloadPolicy,
        *,
        client: httpx.AsyncClient | Any | None = None,
    ) -> None:
        self.root = root.resolve()
        self.policy = policy
        self._client = client
        self._owns_client = client is None
        self._files = 0
        self._bytes = 0
        self._lock = asyncio.Lock()
        self._url_cache: dict[str, MediaArtifact] = {}
        self._digest_paths: dict[tuple[str, str], Path] = {}

    async def __aenter__(self) -> Self:
        self.root.mkdir(parents=True, exist_ok=True)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.policy.timeout_seconds,
                follow_redirects=False,
                headers={"User-Agent": "ContentBot/0.1 (bounded media fetch)"},
            )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def download(
        self,
        url: str,
        *,
        cancellation: CancellationToken,
    ) -> MediaArtifact:
        if self._client is None:
            raise RuntimeError("Media downloader is not open")
        canonical = _safe_url(url, self.policy.allowed_hosts)
        cancellation.raise_if_cancelled()
        async with self._lock:
            cached = self._url_cache.get(canonical)
            if cached is not None and cached.path.is_file():
                return cached
            if self._files >= self.policy.max_files:
                raise CrawlerFailure(
                    CrawlerErrorCode.BUDGET_EXHAUSTED,
                    "Crawler media file budget was exhausted.",
                )
            remaining_total = self.policy.max_total_bytes - self._bytes
            if remaining_total <= 0:
                raise CrawlerFailure(
                    CrawlerErrorCode.BUDGET_EXHAUSTED,
                    "Crawler media byte budget was exhausted.",
                )
            response, final_url = await self._open(canonical, cancellation)
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            if not any(content_type.startswith(prefix) for prefix in self.policy.allowed_content_types):
                await response.aclose()
                raise CrawlerFailure(
                    CrawlerErrorCode.UNSUPPORTED,
                    "Crawler media response type is not allowed.",
                )
            extension = _EXTENSIONS.get(content_type)
            if extension is None:
                await response.aclose()
                raise CrawlerFailure(
                    CrawlerErrorCode.UNSUPPORTED,
                    "Crawler media response format is not supported.",
                )
            try:
                declared = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                declared = 0
            byte_limit = min(self.policy.max_file_bytes, remaining_total)
            if declared < 0 or declared > byte_limit:
                await response.aclose()
                raise CrawlerFailure(
                    CrawlerErrorCode.BUDGET_EXHAUSTED,
                    "Crawler media response exceeds its byte budget.",
                )

            request_id = hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:32]
            source_root = (self.root / self.policy.source_id).resolve()
            source_root.mkdir(parents=True, exist_ok=True)
            final_path = (source_root / f"{request_id}{extension}").resolve()
            if final_path.parent != source_root:
                await response.aclose()
                raise CrawlerFailure(CrawlerErrorCode.STORAGE_ERROR, "Media destination is invalid.")
            temporary = (source_root / f".{request_id}.{uuid.uuid4().hex}.part").resolve()
            digest = hashlib.sha256()
            written = 0
            try:
                with temporary.open("xb") as stream:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        cancellation.raise_if_cancelled()
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > byte_limit:
                            raise CrawlerFailure(
                                CrawlerErrorCode.BUDGET_EXHAUSTED,
                                "Crawler media response exceeded its streaming byte budget.",
                            )
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                digest_text = digest.hexdigest()
                duplicate_path = self._digest_paths.get((content_type, digest_text))
                if duplicate_path is not None and duplicate_path.is_file():
                    temporary.unlink(missing_ok=True)
                    final_path = duplicate_path
                else:
                    os.replace(temporary, final_path)
                    self._digest_paths[(content_type, digest_text)] = final_path
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            finally:
                await response.aclose()

            if final_path.name == f"{request_id}{extension}":
                self._files += 1
            self._bytes += written
            artifact = MediaArtifact(
                source_id=self.policy.source_id,
                request_id=request_id,
                path=final_path,
                byte_count=written,
                sha256=digest_text,
                content_type=content_type,
                source_url=_public_url(final_url),
            )
            self._url_cache[canonical] = artifact
            return artifact

    async def _open(
        self,
        url: str,
        cancellation: CancellationToken,
    ) -> tuple[Any, str]:
        assert self._client is not None
        current = url
        for redirect_index in range(self.policy.max_redirects + 1):
            cancellation.raise_if_cancelled()
            response = await self._request(current)
            status = int(getattr(response, "status_code", 0))
            if status in {301, 302, 303, 307, 308}:
                location = str(response.headers.get("Location") or "")
                await response.aclose()
                if not location or redirect_index >= self.policy.max_redirects:
                    raise CrawlerFailure(
                        CrawlerErrorCode.TRANSPORT_ERROR,
                        "Crawler media redirect policy was exceeded.",
                    )
                current = _safe_url(urljoin(current, location), self.policy.allowed_hosts)
                continue
            if status == 404:
                await response.aclose()
                raise CrawlerFailure(CrawlerErrorCode.NOT_FOUND, "Crawler media was not found.")
            if status == 429:
                await response.aclose()
                raise CrawlerFailure(
                    CrawlerErrorCode.RATE_LIMITED,
                    "Crawler media host rate limit was reached.",
                    retryable=True,
                )
            if status >= 400:
                await response.aclose()
                raise CrawlerFailure(
                    CrawlerErrorCode.TRANSPORT_ERROR,
                    "Crawler media host returned an error.",
                    retryable=status >= 500,
                )
            return response, current
        raise AssertionError("Media redirect loop ended unexpectedly")

    async def _request(self, url: str) -> Any:
        assert self._client is not None
        response: Any | None = None
        for attempt in range(3):
            try:
                request = self._client.build_request("GET", url)
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise CrawlerFailure(
                        CrawlerErrorCode.TRANSPORT_ERROR,
                        "Crawler media transport failed.",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            status = int(getattr(response, "status_code", 0))
            if status not in _RETRYABLE or attempt == 2:
                return response
            await response.aclose()
            await asyncio.sleep(0.25 * (2**attempt))
        assert response is not None
        return response


def _safe_url(value: str, roots: tuple[str, ...]) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CrawlerFailure(CrawlerErrorCode.UNSUPPORTED, "Crawler media URL is not allowed.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CrawlerFailure(CrawlerErrorCode.UNSUPPORTED, "Crawler media URL is invalid.") from exc
    if port not in {None, 443}:
        raise CrawlerFailure(CrawlerErrorCode.UNSUPPORTED, "Crawler media URL port is not allowed.")
    host = _host(parsed.hostname)
    if not any(host == root or host.endswith(f".{root}") for root in roots):
        raise CrawlerFailure(CrawlerErrorCode.PERMISSION_REQUIRED, "Crawler media host is not allowlisted.")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _host(value: str) -> str:
    candidate = str(value).strip().casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        try:
            host = candidate.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("Media host is invalid") from exc
        if not host or any(not label or len(label) > 63 for label in host.split(".")):
            raise ValueError("Media host is invalid")
        return host
    if not address.is_global:
        raise ValueError("Media host must not be private or local")
    return address.compressed.casefold()


def _public_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
