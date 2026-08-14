"""Load reviewed DOM search contracts collected from owned browser profiles.

No selectors are bundled here.  A contract is an independently observed,
reviewed project artifact and remains disabled when the artifact is absent or
invalid.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import quote_plus, urlsplit

from .adapters.browser_video_dom import BrowserVideoDomContract
from .adapters.weibo import WeiboDomContract

DOM_CONTRACT_SCHEMA = "cbce.observed-dom-search.v1"
MAX_CONTRACT_BYTES = 65_536
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_REVIEWER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@ -]{0,99}")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "source_id",
        "provider_id",
        "mode",
        "observed_at",
        "evidence_digest",
        "reviewed_by",
        "search_url_template",
        "login_selectors",
        "selectors",
    }
)


@dataclass(frozen=True, slots=True)
class ObservedDomSourceSpec:
    source_id: str
    provider_id: str
    mode: str
    search_hosts: tuple[str, ...]
    metric_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ObservedDomSearchContract:
    source_id: str
    provider_id: str
    mode: str
    observed_at: datetime
    evidence_digest: str
    reviewed_by: str
    search_url_template: str
    login_selectors: tuple[str, ...]
    selectors: BrowserVideoDomContract | WeiboDomContract
    artifact_digest: str
    artifact_path: Path

    def build_search_url(self, query: str, page: int) -> str:
        if not query.strip() or not 1 <= page <= 10_000:
            raise ValueError("DOM search query or page is invalid")
        rendered = self.search_url_template.format(
            query=quote_plus(query.strip()), page=page
        )
        _validate_search_url(rendered, DOM_SOURCE_SPECS[self.source_id])
        return rendered


DOM_SOURCE_SPECS: dict[str, ObservedDomSourceSpec] = {
    "xhs": ObservedDomSourceSpec(
        "xhs",
        "cbce_xhs",
        "browser_video_v1",
        ("xiaohongshu.com", "rednote.com"),
        frozenset({"like_count", "comment_count", "favorite_count"}),
    ),
    "douyin": ObservedDomSourceSpec(
        "douyin",
        "cbce_douyin",
        "browser_video_v1",
        ("douyin.com",),
        frozenset({"like_count", "comment_count", "share_count", "view_count"}),
    ),
    "kuaishou": ObservedDomSourceSpec(
        "kuaishou",
        "cbce_kuaishou",
        "browser_video_v1",
        ("kuaishou.com",),
        frozenset({"like_count", "comment_count", "share_count", "view_count"}),
    ),
    "weibo": ObservedDomSourceSpec(
        "weibo",
        "cbce_weibo",
        "weibo_post_v1",
        ("weibo.com", "weibo.cn"),
        frozenset({"like_count", "comment_count", "share_count"}),
    ),
    "zhihu": ObservedDomSourceSpec(
        "zhihu",
        "cbce_zhihu",
        "browser_video_v1",
        ("zhihu.com",),
        frozenset({"like_count", "comment_count", "favorite_count"}),
    ),
}


class DomContractUnavailable(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.safe_message = message


def contract_path(contract_root: Path, source_id: str) -> Path:
    spec = _source_spec(source_id)
    root = contract_root.expanduser().resolve()
    return root / f"{spec.source_id}.search.json"


def load_observed_dom_contract(
    contract_root: Path,
    source_id: str,
    *,
    now: datetime | None = None,
) -> ObservedDomSearchContract:
    spec = _source_spec(source_id)
    root = contract_root.expanduser().resolve()
    path = contract_path(root, source_id)
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_MISSING",
            f"Reviewed {spec.source_id} DOM search contract is missing.",
        ) from exc
    if resolved.parent != root or path.is_symlink() or not resolved.is_file():
        raise DomContractUnavailable(
            "DOM_CONTRACT_PATH_INVALID",
            "DOM contract must be a regular file directly under the contract root.",
        )
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_UNREADABLE", "DOM contract could not be inspected."
        ) from exc
    if not 1 <= size <= MAX_CONTRACT_BYTES:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SIZE_INVALID", "DOM contract size is invalid."
        )
    try:
        raw = resolved.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_INVALID_JSON", "DOM contract is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SCHEMA_INVALID", "DOM contract fields do not match the schema."
        )
    if (
        payload.get("schema_version") != DOM_CONTRACT_SCHEMA
        or payload.get("source_id") != spec.source_id
        or payload.get("provider_id") != spec.provider_id
        or payload.get("mode") != spec.mode
    ):
        raise DomContractUnavailable(
            "DOM_CONTRACT_IDENTITY_INVALID",
            "DOM contract identity does not match the selected provider.",
        )
    observed_at = _observed_at(payload.get("observed_at"), now=now)
    evidence_digest = str(payload.get("evidence_digest") or "").casefold()
    reviewed_by = str(payload.get("reviewed_by") or "").strip()
    if not _HEX_DIGEST.fullmatch(evidence_digest) or not _REVIEWER.fullmatch(
        reviewed_by
    ):
        raise DomContractUnavailable(
            "DOM_CONTRACT_PROVENANCE_INVALID",
            "DOM contract provenance metadata is invalid.",
        )
    template = str(payload.get("search_url_template") or "")
    _validate_template(template, spec)
    login_selectors = _login_selectors(payload.get("login_selectors"))
    selectors_payload = payload.get("selectors")
    if not isinstance(selectors_payload, dict):
        raise DomContractUnavailable(
            "DOM_CONTRACT_SELECTOR_SCHEMA_INVALID",
            "DOM contract selectors must be an object.",
        )
    selectors = _selectors(spec, selectors_payload)
    return ObservedDomSearchContract(
        source_id=spec.source_id,
        provider_id=spec.provider_id,
        mode=spec.mode,
        observed_at=observed_at,
        evidence_digest=evidence_digest,
        reviewed_by=reviewed_by,
        search_url_template=template,
        login_selectors=login_selectors,
        selectors=selectors,
        artifact_digest=hashlib.sha256(raw).hexdigest(),
        artifact_path=resolved,
    )


def _source_spec(source_id: str) -> ObservedDomSourceSpec:
    try:
        return DOM_SOURCE_SPECS[str(source_id).strip().casefold()]
    except KeyError as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SOURCE_UNSUPPORTED",
            "Source does not support reviewed DOM search contracts.",
        ) from exc


def _observed_at(value: Any, *, now: datetime | None) -> datetime:
    try:
        observed = datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_PROVENANCE_INVALID",
            "DOM contract observed_at is invalid.",
        ) from exc
    if observed.tzinfo is None:
        raise DomContractUnavailable(
            "DOM_CONTRACT_PROVENANCE_INVALID",
            "DOM contract observed_at must include a timezone.",
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    observed = observed.astimezone(UTC)
    if observed > current.astimezone(UTC) + timedelta(minutes=5):
        raise DomContractUnavailable(
            "DOM_CONTRACT_PROVENANCE_INVALID",
            "DOM contract observed_at cannot be in the future.",
        )
    return observed


def _validate_template(template: str, spec: ObservedDomSourceSpec) -> None:
    if not template or len(template) > 2_048 or any(
        character in template for character in ("\x00", "\r", "\n")
    ):
        raise DomContractUnavailable(
            "DOM_CONTRACT_SEARCH_URL_INVALID", "DOM search URL template is invalid."
        )
    fields: list[str] = []
    try:
        for _literal, field_name, format_spec, conversion in Formatter().parse(
            template
        ):
            if field_name is None:
                continue
            if field_name not in {"query", "page"} or format_spec or conversion:
                raise ValueError
            fields.append(field_name)
    except ValueError as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SEARCH_URL_INVALID", "DOM search URL template is invalid."
        ) from exc
    if fields.count("query") != 1 or fields.count("page") > 1:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SEARCH_URL_INVALID",
            "DOM search URL template must contain one query placeholder.",
        )
    try:
        rendered = template.format(query="contract-check", page=1)
    except (KeyError, ValueError) as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SEARCH_URL_INVALID", "DOM search URL template is invalid."
        ) from exc
    _validate_search_url(rendered, spec)


def _validate_search_url(value: str, spec: ObservedDomSourceSpec) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SEARCH_URL_INVALID", "DOM search URL is invalid."
        ) from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    allowed = any(
        host == root or host.endswith(f".{root}") for root in spec.search_hosts
    )
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.fragment
    ):
        raise DomContractUnavailable(
            "DOM_CONTRACT_SEARCH_URL_INVALID",
            "DOM search URL must use an approved HTTPS platform host.",
        )


def _selectors(
    spec: ObservedDomSourceSpec, payload: dict[str, Any]
) -> BrowserVideoDomContract | WeiboDomContract:
    try:
        if spec.mode == "weibo_post_v1":
            allowed = {
                "root_selector",
                "card_selector",
                "link_selector",
                "text_selector",
                "author_selector",
                "timestamp_selector",
                "next_selector",
                "metric_selectors",
                "image_selector",
            }
            if set(payload) - allowed:
                raise ValueError
            contract = WeiboDomContract(**payload)
        else:
            allowed = {
                "root_selector",
                "card_selector",
                "link_selector",
                "title_selector",
                "next_selector",
                "body_selector",
                "author_selector",
                "author_attribute",
                "timestamp_selector",
                "metric_selectors",
                "image_selector",
                "cover_selector",
            }
            if set(payload) - allowed:
                raise ValueError
            contract = BrowserVideoDomContract(**payload)
    except (TypeError, ValueError) as exc:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SELECTOR_SCHEMA_INVALID",
            "DOM contract selector schema is invalid.",
        ) from exc
    metric_ids = frozenset(contract.metric_selectors)
    if not metric_ids or not metric_ids.issubset(spec.metric_ids):
        raise DomContractUnavailable(
            "DOM_CONTRACT_SELECTOR_SCHEMA_INVALID",
            "DOM contract metrics are invalid for the selected source.",
        )
    return contract


def _login_selectors(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 10:
        raise DomContractUnavailable(
            "DOM_CONTRACT_SELECTOR_SCHEMA_INVALID",
            "DOM contract login selectors are invalid.",
        )
    selectors = tuple(str(selector).strip() for selector in value)
    if any(
        not selector
        or len(selector) > 300
        or any(ord(character) < 32 for character in selector)
        for selector in selectors
    ):
        raise DomContractUnavailable(
            "DOM_CONTRACT_SELECTOR_SCHEMA_INVALID",
            "DOM contract login selectors are invalid.",
        )
    return selectors
