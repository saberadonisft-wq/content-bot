"""Runtime guard for the non-commercial licensed reuse zone."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class LicensedReuseError(RuntimeError):
    """Raised when a licensed provider is used outside the approved purpose."""


@dataclass(frozen=True, slots=True)
class LicensedReusePolicy:
    """Small immutable policy object passed to a licensed adapter."""

    non_commercial_learning: bool = True
    allow_large_scale: bool = False
    allow_challenge_solving: bool = False
    allow_proxy_rotation: bool = False
    max_items: int = 100
    max_requests: int = 100

    def validate(self) -> None:
        if not self.non_commercial_learning:
            raise LicensedReuseError(
                "MediaCrawler-derived code is restricted to non-commercial learning/research."
            )
        if self.allow_large_scale:
            raise LicensedReuseError(
                "Large-scale crawling is disabled for the licensed provider."
            )
        if self.allow_challenge_solving:
            raise LicensedReuseError(
                "Challenge solving is disabled for the licensed provider."
            )
        if self.allow_proxy_rotation:
            raise LicensedReuseError(
                "Proxy rotation is disabled for the licensed provider."
            )

    def validate_budgets(self, *, max_items: int, max_requests: int) -> None:
        self.validate()
        if max_items > self.max_items or max_requests > self.max_requests:
            raise LicensedReuseError(
                "Licensed provider budgets are capped for non-commercial learning use."
            )


def assert_licensed_reuse_allowed(
    settings: Mapping[str, object] | None = None,
) -> LicensedReusePolicy:
    """Validate explicit runtime settings and return the immutable policy.

    The default is intentionally safe for the current non-commercial project.
    A future commercial build must set the flag false and therefore fail closed
    until the derived provider is replaced or separately relicensed.
    """

    values = settings or {}
    policy = LicensedReusePolicy(
        non_commercial_learning=_as_bool(
            values.get("non_commercial_learning", True)
        ),
        allow_large_scale=_as_bool(values.get("allow_large_scale", False)),
        allow_challenge_solving=_as_bool(
            values.get("allow_challenge_solving", False)
        ),
        allow_proxy_rotation=_as_bool(values.get("allow_proxy_rotation", False)),
    )
    policy.validate()
    return policy


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)
