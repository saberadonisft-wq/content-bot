"""Boundary-safe URL and source matching for crawler targets."""

from __future__ import annotations

import ipaddress
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .contracts import DomainRule, SourceManifest, TargetMatch


class InvalidTargetUrl(ValueError):
    """The supplied value is not a safe, absolute HTTP(S) target URL."""


class AmbiguousTargetUrl(ValueError):
    """More than one source claims the same target host."""


def _reject_control_characters(value: str) -> None:
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise InvalidTargetUrl("Target URL contains control characters")


def normalize_host(value: str) -> str:
    """Return a lower-case ASCII DNS name (or normalized IP address)."""

    candidate = value.strip().rstrip(".").lower()
    if not candidate:
        raise InvalidTargetUrl("Target URL has no host")
    _reject_control_characters(candidate)
    try:
        return ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        pass
    try:
        normalized = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidTargetUrl("Target URL has an invalid host") from exc
    if len(normalized) > 253:
        raise InvalidTargetUrl("Target URL host is too long")
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise InvalidTargetUrl("Target URL has an invalid host")
    return normalized


def host_matches_rule(host: str, rule: DomainRule) -> bool:
    """Match a host on complete DNS labels, never by an unsafe suffix."""

    normalized_host = normalize_host(host)
    normalized_root = normalize_host(rule.root)
    return normalized_host == normalized_root or (
        rule.include_subdomains
        and normalized_host.endswith(f".{normalized_root}")
    )


def domain_rules_overlap(left: DomainRule, right: DomainRule) -> bool:
    """Return whether two rules could claim at least one identical host."""

    left_root = normalize_host(left.root)
    right_root = normalize_host(right.root)
    if left_root == right_root:
        return True
    if left.include_subdomains and right_root.endswith(f".{left_root}"):
        return True
    return right.include_subdomains and left_root.endswith(f".{right_root}")


def normalize_target_url(value: str) -> tuple[str, str]:
    """Validate and minimally normalize an absolute public target URL.

    Query strings are preserved because they may identify a feed.  Fragments are
    removed because they are never sent to a server.  User information and
    backslashes are rejected to avoid ambiguous authority parsing.
    """

    if value != value.strip() or "\\" in value:
        raise InvalidTargetUrl("Target URL is malformed")
    _reject_control_characters(value)
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidTargetUrl("Target URL must use http or https")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise InvalidTargetUrl("Target URL must have a host and no user information")
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidTargetUrl("Target URL has an invalid port") from exc
    host = normalize_host(parsed.hostname or "")
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None or default_port else f"{display_host}:{port}"
    normalized = SplitResult(
        scheme=parsed.scheme.lower(),
        netloc=netloc,
        path=parsed.path or "/",
        query=parsed.query,
        fragment="",
    )
    return urlunsplit(normalized), host


def _configured_host(value: str) -> str:
    if "://" in value:
        _, host = normalize_target_url(value)
        return host
    return normalize_host(value)


def match_target(
    value: str,
    manifests: Sequence[SourceManifest],
    *,
    configured_hosts: Mapping[str, Iterable[str]] | None = None,
) -> TargetMatch | None:
    """Match a URL against static rules and exact runtime-configured hosts.

    Runtime hosts are exact by design.  This supports allowlisted feeds and
    validated Mastodon instances without turning either source into a catch-all.
    """

    normalized_url, host = normalize_target_url(value)
    candidates: dict[str, tuple[str, bool]] = {}
    for manifest in manifests:
        for rule in manifest.domain_rules:
            if host_matches_rule(host, rule):
                candidates[manifest.id] = (normalize_host(rule.root), False)
                break

    for source_id, values in (configured_hosts or {}).items():
        for configured in values:
            configured_host = _configured_host(configured)
            if host == configured_host:
                candidates[source_id] = (configured_host, True)
                break

    if not candidates:
        return None
    if len(candidates) > 1:
        claimed = ", ".join(sorted(candidates))
        raise AmbiguousTargetUrl(f"Target host is claimed by multiple sources: {claimed}")
    source_id, (matched_domain, is_configured) = next(iter(candidates.items()))
    return TargetMatch(
        source_id=source_id,
        normalized_url=normalized_url,
        host=host,
        matched_domain=matched_domain,
        configured_domain=is_configured,
    )
