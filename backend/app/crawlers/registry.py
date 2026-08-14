"""Validated immutable view over the canonical source manifests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import (
    Coverage,
    ImplementationState,
    Operation,
    ProviderOperationSpec,
    SourceManifest,
)
from .manifests import SOURCE_MANIFESTS
from .target_detection import domain_rules_overlap, match_target

CANONICAL_SOURCE_IDS = (
    "youtube", "web", "steam", "bluesky", "mastodon", "reddit", "x",
    "xhs", "douyin", "kuaishou", "bilibili", "weibo", "tieba", "zhihu",
    "tiktok", "facebook", "instagram",
)


class SourceRegistry:
    def __init__(self, manifests: Iterable[SourceManifest]) -> None:
        self.manifests = tuple(manifests)
        self._by_id = {manifest.id: manifest for manifest in self.manifests}
        self._aliases = {
            alias: manifest.id
            for manifest in self.manifests
            for alias in manifest.legacy_aliases
        }
        self._validate()

    def _validate(self) -> None:
        ids = tuple(manifest.id for manifest in self.manifests)
        if ids != CANONICAL_SOURCE_IDS:
            raise ValueError(f"Source manifest order mismatch: {ids!r}")
        if len(self._by_id) != len(self.manifests):
            raise ValueError("Duplicate source ID")
        expected_orders = tuple(range(1, len(self.manifests) + 1))
        if tuple(manifest.order for manifest in self.manifests) != expected_orders:
            raise ValueError("Source orders must be contiguous and deterministic")
        occupied = set(self._by_id)
        for alias, canonical in self._aliases.items():
            if not alias or alias in occupied or alias == canonical:
                raise ValueError(f"Invalid or duplicate source alias: {alias}")
            occupied.add(alias)
        rules: list[tuple[str, object]] = []
        for manifest in self.manifests:
            provider_ids = [provider.id for provider in manifest.providers]
            if len(provider_ids) != len(set(provider_ids)):
                raise ValueError(f"Duplicate provider in {manifest.id}")
            if manifest.default_provider_id not in provider_ids:
                raise ValueError(f"Missing default provider for {manifest.id}")
            for provider in manifest.providers:
                operations = [spec.operation for spec in provider.operations]
                if len(operations) != len(set(operations)):
                    raise ValueError(f"Duplicate operation in {manifest.id}/{provider.id}")
                for spec in provider.operations:
                    if spec.implementation is ImplementationState.IMPLEMENTED and not spec.handler_key:
                        raise ValueError(f"Implemented operation lacks handler: {manifest.id}/{provider.id}/{spec.operation}")
                    if spec.coverage is Coverage.UNSUPPORTED and spec.implementation is ImplementationState.IMPLEMENTED:
                        raise ValueError("Unsupported operation cannot be implemented")
            for rule in manifest.domain_rules:
                for owner, previous in rules:
                    if owner != manifest.id and domain_rules_overlap(rule, previous):
                        raise ValueError(f"Domain rule overlap: {owner} and {manifest.id}")
                rules.append((manifest.id, rule))

    def __iter__(self):
        return iter(self.manifests)

    def get(self, source_id: str) -> SourceManifest | None:
        return self._by_id.get(self.resolve_id(source_id))

    def require(self, source_id: str) -> SourceManifest:
        manifest = self.get(source_id)
        if manifest is None:
            raise KeyError(source_id)
        return manifest

    def resolve_id(self, source_id: str) -> str:
        normalized = str(source_id).strip().casefold()
        return self._aliases.get(normalized, normalized)

    def operation_specs(self, source_id: str, operation: Operation) -> tuple[tuple[str, ProviderOperationSpec], ...]:
        manifest = self.require(source_id)
        return tuple(
            (provider.id, spec)
            for provider in manifest.providers
            for spec in provider.operations
            if spec.operation is operation
        )

    def executable(self, source_id: str, operation: Operation) -> bool:
        return any(
            spec.implementation is ImplementationState.IMPLEMENTED
            and spec.coverage is not Coverage.UNSUPPORTED
            and bool(spec.handler_key)
            for _, spec in self.operation_specs(source_id, operation)
        )

    def executable_provider(
        self,
        source_id: str,
        operation: Operation,
        *,
        preferred_provider_id: str | None = None,
    ) -> tuple[str, ProviderOperationSpec]:
        """Resolve the actual provider that owns an executable operation.

        A source default can legitimately own a different operation (for
        example X embed versus X search), so checkpoint provenance must not use
        ``default_provider_id`` blindly.
        """
        manifest = self.require(source_id)
        candidates = tuple(
            (provider_id, spec)
            for provider_id, spec in self.operation_specs(source_id, operation)
            if spec.implementation is ImplementationState.IMPLEMENTED
            and spec.coverage is not Coverage.UNSUPPORTED
            and bool(spec.handler_key)
        )
        if preferred_provider_id:
            for candidate in candidates:
                if candidate[0] == preferred_provider_id:
                    return candidate
            raise ValueError(
                f"Provider {preferred_provider_id!r} cannot execute "
                f"{source_id!r}/{operation.value!r}"
            )
        for candidate in candidates:
            if candidate[0] == manifest.default_provider_id:
                return candidate
        if candidates:
            return candidates[0]
        raise ValueError(f"No executable provider for {source_id!r}/{operation.value!r}")

    def match_target(self, value: str, *, configured_hosts=None):
        return match_target(value, self.manifests, configured_hosts=configured_hosts)

    def validate_bindings(
        self,
        connectors: Mapping[str, Any],
        channel_handlers: Mapping[str, Any],
        *,
        renderer_ids: Iterable[str] = (),
    ) -> None:
        """Fail fast if a statically implemented action has no callable owner."""
        renderers = set(renderer_ids)
        for manifest in self.manifests:
            for provider in manifest.providers:
                for spec in provider.operations:
                    if spec.implementation is not ImplementationState.IMPLEMENTED:
                        continue
                    handler = spec.handler_key or ""
                    parts = handler.split(":")
                    if len(parts) == 3 and parts[0] == "connector":
                        connector = connectors.get(parts[1])
                        if connector is None or not callable(getattr(connector, parts[2], None)):
                            raise ValueError(f"Missing connector binding: {handler}")
                    elif len(parts) == 2 and parts[0] == "channel":
                        if not callable(channel_handlers.get(parts[1])):
                            raise ValueError(f"Missing channel binding: {handler}")
                    elif len(parts) == 2 and parts[0] == "renderer":
                        if parts[1] not in renderers:
                            raise ValueError(f"Missing renderer binding: {handler}")
                    else:
                        raise ValueError(f"Unknown handler binding: {handler}")


SOURCE_REGISTRY = SourceRegistry(SOURCE_MANIFESTS)
