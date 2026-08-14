"""Value-free DOM structure observations for clean-room browser adapters."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

_SAFE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,79}$")
_OPAQUE_TOKEN = re.compile(r"(?:[A-Fa-f0-9]{16,}|[A-Za-z0-9_-]{24,})")
_MAX_LANDMARKS = 250
_SAFE_PATH_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SAFE_PATH_PREFIX = re.compile(r"^/[A-Za-z0-9_./-]{1,99}$")
_SAFE_SENTINEL_SELECTOR = re.compile(
    r"^[a-z][a-z0-9-]{0,39}(?:\.[A-Za-z_][A-Za-z0-9_-]{0,79}){1,3}$"
)

_OBSERVE_SCRIPT = r"""
(config) => {
  const counts = new Map();
  const attributeNames = new Map();
  let elementCount = 0;
  let openShadowRootCount = 0;
  const visit = (root) => {
    const elements = Array.from(root.querySelectorAll('*')).slice(0, 20000);
    for (const element of elements) {
      elementCount += 1;
      const tag = element.tagName.toLowerCase();
      counts.set(tag, (counts.get(tag) || 0) + 1);
      for (const className of Array.from(element.classList).slice(0, 20)) {
        const key = `${tag}.${className}`;
        counts.set(key, (counts.get(key) || 0) + 1);
      }
      for (const attribute of Array.from(element.attributes).slice(0, 30)) {
        attributeNames.set(attribute.name, (attributeNames.get(attribute.name) || 0) + 1);
      }
      if (element.shadowRoot) {
        openShadowRootCount += 1;
        visit(element.shadowRoot);
      }
    }
  };
  visit(document);
  const linkPathCounts = {};
  for (const name of Object.keys(config.link_path_prefixes || {})) {
    linkPathCounts[name] = 0;
  }
  for (const anchor of Array.from(document.querySelectorAll('a[href]')).slice(0, 20000)) {
    let parsed;
    try { parsed = new URL(anchor.href, document.location.href); } catch (_) { continue; }
    const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
    const allowed = (config.allowed_hosts || []).some(
      (root) => host === root || host.endsWith(`.${root}`)
    );
    if (!allowed) continue;
    for (const [name, prefix] of Object.entries(config.link_path_prefixes || {})) {
      if (parsed.pathname.startsWith(prefix)) linkPathCounts[name] += 1;
    }
  }
  const sentinelCounts = {};
  for (const [name, selector] of Object.entries(config.sentinel_selectors || {})) {
    sentinelCounts[name] = Math.min(document.querySelectorAll(selector).length, 20000);
  }
  return {
    element_count: elementCount,
    open_shadow_root_count: openShadowRootCount,
    landmarks: Array.from(counts.entries()),
    attribute_names: Array.from(attributeNames.entries()),
    link_path_counts: linkPathCounts,
    sentinel_counts: sentinelCounts,
  };
}
"""


@dataclass(frozen=True, slots=True)
class DomLandmark:
    signature: str
    count: int


@dataclass(frozen=True, slots=True)
class DomStructureObservation:
    source_id: str
    provider_id: str
    operation: str
    final_host: str
    element_count: int
    open_shadow_root_count: int
    landmarks: tuple[DomLandmark, ...]
    attribute_names: tuple[DomLandmark, ...]
    link_path_counts: tuple[DomLandmark, ...]
    sentinel_counts: tuple[DomLandmark, ...]
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "cbce.dom-structure.v1",
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "operation": self.operation,
            "final_host": self.final_host,
            "element_count": self.element_count,
            "open_shadow_root_count": self.open_shadow_root_count,
            "landmarks": [
                {"signature": item.signature, "count": item.count}
                for item in self.landmarks
            ],
            "attribute_names": [
                {"signature": item.signature, "count": item.count}
                for item in self.attribute_names
            ],
            "link_path_counts": [
                {"signature": item.signature, "count": item.count}
                for item in self.link_path_counts
            ],
            "sentinel_counts": [
                {"signature": item.signature, "count": item.count}
                for item in self.sentinel_counts
            ],
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
        }


async def observe_dom_structure(
    page: Any,
    *,
    source_id: str,
    provider_id: str,
    operation: str,
    allowed_hosts: tuple[str, ...],
    link_path_prefixes: Mapping[str, str] | None = None,
    sentinel_selectors: Mapping[str, str] | None = None,
) -> DomStructureObservation:
    """Collect bounded structural names; never read text, URLs or attribute values."""

    host = (urlsplit(str(page.url)).hostname or "").casefold().rstrip(".")
    if not _allowed_host(host, allowed_hosts):
        raise ValueError("DOM observation page is outside the approved source domains")
    safe_prefixes = _path_prefixes(link_path_prefixes or {})
    safe_sentinels = _sentinel_selectors(sentinel_selectors or {})
    raw = await page.evaluate(
        _OBSERVE_SCRIPT,
        {
            "allowed_hosts": [root.casefold().rstrip(".") for root in allowed_hosts],
            "link_path_prefixes": safe_prefixes,
            "sentinel_selectors": safe_sentinels,
        },
    )
    if not isinstance(raw, Mapping):
        raise TypeError("DOM observation result is invalid")
    return DomStructureObservation(
        source_id=_identity(source_id, "source"),
        provider_id=_identity(provider_id, "provider"),
        operation=_identity(operation, "operation"),
        final_host=host,
        element_count=_bounded_count(raw.get("element_count"), maximum=100_000),
        open_shadow_root_count=_bounded_count(
            raw.get("open_shadow_root_count"), maximum=10_000
        ),
        landmarks=_landmarks(raw.get("landmarks"), allow_compound=True),
        attribute_names=_landmarks(
            raw.get("attribute_names"), allow_compound=False
        ),
        link_path_counts=_named_counts(raw.get("link_path_counts"), safe_prefixes),
        sentinel_counts=_named_counts(raw.get("sentinel_counts"), safe_sentinels),
    )


def _landmarks(value: Any, *, allow_compound: bool) -> tuple[DomLandmark, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("DOM structural landmarks must be an array")
    result: list[DomLandmark] = []
    seen: set[str] = set()
    for row in value[:20_000]:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        raw_signature, raw_count = row
        signature = str(raw_signature).strip()
        parts = signature.split(".", 1) if allow_compound else [signature]
        if (
            not parts
            or any(not _SAFE_TOKEN.fullmatch(part) for part in parts)
            or any(_OPAQUE_TOKEN.search(part) for part in parts)
        ):
            continue
        normalized = ".".join(part.casefold() for part in parts)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(
            DomLandmark(
                normalized,
                _bounded_count(raw_count, maximum=100_000),
            )
        )
    result.sort(key=lambda item: (-item.count, item.signature))
    return tuple(result[:_MAX_LANDMARKS])


def _bounded_count(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise TypeError("DOM observation count is invalid")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("DOM observation count is invalid") from exc
    if not 0 <= count <= maximum:
        raise ValueError("DOM observation count exceeds its bound")
    return count


def _path_prefixes(value: Mapping[str, str]) -> dict[str, str]:
    if len(value) > 20:
        raise ValueError("DOM link path prefix contract exceeds its bound")
    result: dict[str, str] = {}
    for raw_name, raw_prefix in value.items():
        name = str(raw_name).strip().casefold()
        prefix = str(raw_prefix).strip()
        if not _SAFE_PATH_NAME.fullmatch(name) or not _SAFE_PATH_PREFIX.fullmatch(prefix):
            raise ValueError("DOM link path prefix contract is invalid")
        result[name] = prefix
    return result


def _named_counts(value: Any, contract: Mapping[str, str]) -> tuple[DomLandmark, ...]:
    if not isinstance(value, Mapping):
        raise TypeError("DOM link path counts must be an object")
    if set(value) != set(contract):
        raise ValueError("DOM link path counts do not match the declared contract")
    return tuple(
        DomLandmark(name, _bounded_count(value[name], maximum=20_000))
        for name in sorted(contract)
    )


def _sentinel_selectors(value: Mapping[str, str]) -> dict[str, str]:
    if len(value) > 20:
        raise ValueError("DOM sentinel selector contract exceeds its bound")
    result: dict[str, str] = {}
    for raw_name, raw_selector in value.items():
        name = str(raw_name).strip().casefold()
        selector = str(raw_selector).strip()
        if not _SAFE_PATH_NAME.fullmatch(name) or not _SAFE_SENTINEL_SELECTOR.fullmatch(
            selector
        ):
            raise ValueError("DOM sentinel selector contract is invalid")
        result[name] = selector
    return result


def _identity(value: str, label: str) -> str:
    normalized = str(value).strip().casefold()
    if not _SAFE_TOKEN.fullmatch(normalized):
        raise ValueError(f"DOM observation {label} is invalid")
    return normalized


def _allowed_host(host: str, roots: tuple[str, ...]) -> bool:
    return any(host == root or host.endswith(f".{root}") for root in roots)
