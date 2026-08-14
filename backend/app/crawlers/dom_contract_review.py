"""Validate a value-free DOM observation and atomically publish a reviewed contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import settings
from .observed_dom_contract import (
    DOM_CONTRACT_SCHEMA,
    DOM_SOURCE_SPECS,
    MAX_CONTRACT_BYTES,
    load_observed_dom_contract,
)

_OBSERVATION_SCHEMA = "cbce.dom-structure.v1"
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "source_id",
        "provider_id",
        "operation",
        "final_host",
        "element_count",
        "open_shadow_root_count",
        "landmarks",
        "attribute_names",
        "link_path_counts",
        "sentinel_counts",
        "observed_at",
    }
)
_DRAFT_FIELDS = frozenset(
    {"search_url_template", "login_selectors", "selectors"}
)
_SIGNATURE = re.compile(r"[a-z_][a-z0-9_.-]{0,160}")
_OPAQUE_SIGNATURE_TOKEN = re.compile(
    r"(?:[a-f0-9]{16,}|[a-z0-9_-]{24,})", re.IGNORECASE
)


def publish_reviewed_dom_contract(
    observation_path: Path,
    draft_path: Path,
    *,
    contract_root: Path,
    reviewed_by: str,
) -> Path:
    """Publish only after both inputs and the final runtime contract validate."""

    observation_raw = _read_bounded_json_bytes(observation_path, "observation")
    observation = _decode_object(observation_raw, "observation")
    source_id, observed_at = _validate_observation(observation)
    draft_raw = _read_bounded_json_bytes(draft_path, "draft")
    draft = _decode_object(draft_raw, "draft")
    if set(draft) != _DRAFT_FIELDS:
        raise ValueError("DOM contract draft fields do not match the review schema")

    spec = DOM_SOURCE_SPECS[source_id]
    contract = {
        "schema_version": DOM_CONTRACT_SCHEMA,
        "source_id": source_id,
        "provider_id": spec.provider_id,
        "mode": spec.mode,
        "observed_at": observed_at.isoformat(),
        "evidence_digest": hashlib.sha256(observation_raw).hexdigest(),
        "reviewed_by": str(reviewed_by).strip(),
        "search_url_template": draft["search_url_template"],
        "login_selectors": draft["login_selectors"],
        "selectors": draft["selectors"],
    }
    serialized = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(serialized) > MAX_CONTRACT_BYTES:
        raise ValueError("Reviewed DOM contract exceeds its size bound")

    configured_root = contract_root.expanduser()
    if configured_root.is_symlink():
        raise ValueError("DOM contract root cannot be a symlink")
    root = configured_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Validate in an isolated directory before touching an existing contract.
    with tempfile.TemporaryDirectory(prefix="cbce-contract-review-", dir=root) as value:
        validation_root = Path(value)
        candidate = validation_root / f"{source_id}.search.json"
        candidate.write_bytes(serialized)
        load_observed_dom_contract(
            validation_root,
            source_id,
            now=datetime.now(UTC),
        )

    destination = root / f"{source_id}.search.json"
    temporary = root / f".{source_id}.search.{os.getpid()}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    load_observed_dom_contract(root, source_id, now=datetime.now(UTC))
    return destination


def _read_bounded_json_bytes(path: Path, label: str) -> bytes:
    resolved = path.expanduser().resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        raise ValueError(f"DOM {label} must be a regular file")
    size = resolved.stat().st_size
    if not 1 <= size <= MAX_CONTRACT_BYTES:
        raise ValueError(f"DOM {label} size is invalid")
    return resolved.read_bytes()


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"DOM {label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TypeError(f"DOM {label} must be a JSON object")
    return value


def _validate_observation(value: dict[str, Any]) -> tuple[str, datetime]:
    if set(value) != _OBSERVATION_FIELDS or value.get("schema_version") != _OBSERVATION_SCHEMA:
        raise ValueError("DOM observation fields do not match the value-free schema")
    source_id = str(value.get("source_id") or "").casefold()
    try:
        spec = DOM_SOURCE_SPECS[source_id]
    except KeyError as exc:
        raise ValueError("DOM observation source is unsupported") from exc
    if value.get("provider_id") != spec.provider_id or value.get("operation") != "search":
        raise ValueError("DOM observation identity does not match the reviewed provider")
    host = str(value.get("final_host") or "").casefold().rstrip(".")
    if not any(host == root or host.endswith(f".{root}") for root in spec.search_hosts):
        raise ValueError("DOM observation final host is outside the source boundary")
    _bounded_integer(value.get("element_count"), 100_000)
    _bounded_integer(value.get("open_shadow_root_count"), 10_000)
    for field_name in (
        "landmarks",
        "attribute_names",
        "link_path_counts",
        "sentinel_counts",
    ):
        _validate_counts(value.get(field_name), field_name)
    try:
        observed_at = datetime.fromisoformat(str(value.get("observed_at") or ""))
    except ValueError as exc:
        raise ValueError("DOM observation time is invalid") from exc
    if observed_at.tzinfo is None:
        raise ValueError("DOM observation time must include a timezone")
    observed_at = observed_at.astimezone(UTC)
    if observed_at > datetime.now(UTC):
        raise ValueError("DOM observation time cannot be in the future")
    return source_id, observed_at


def _validate_counts(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or len(value) > 250:
        raise ValueError(f"DOM observation {field_name} is invalid")
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"signature", "count"}:
            raise ValueError(f"DOM observation {field_name} is invalid")
        signature = str(row["signature"])
        if (
            not _SIGNATURE.fullmatch(signature)
            or _OPAQUE_SIGNATURE_TOKEN.search(signature)
            or signature in seen
        ):
            raise ValueError(f"DOM observation {field_name} signature is invalid")
        seen.add(signature)
        _bounded_integer(row["count"], 100_000)


def _bounded_integer(value: Any, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError("DOM observation count is invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a reviewed value-free DOM search contract."
    )
    parser.add_argument("--observation", required=True, type=Path)
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--reviewed-by", required=True)
    args = parser.parse_args(argv)
    try:
        destination = publish_reviewed_dom_contract(
            args.observation,
            args.draft,
            contract_root=settings.content_bot_cbce_contract_root,
            reviewed_by=args.reviewed_by,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"CBCE_DOM_CONTRACT_REJECTED {exc}")
        return 2
    print(f"CBCE_DOM_CONTRACT_READY source={destination.name.removesuffix('.search.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
