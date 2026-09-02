"""Atomic content-addressed persistence for cross-provider identity registries."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
from uuid import uuid4

from ._scenario_random import content_id
from .identity import IdentityRegistry


__all__ = ("load_identity_registry", "save_identity_registry")


def save_identity_registry(
    registry: IdentityRegistry, path: str | os.PathLike[str]
) -> Path:
    if not isinstance(registry, IdentityRegistry):
        raise ValueError("registry must be an IdentityRegistry")
    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise ValueError("identity registry path must end in .json")
    record = _record(registry)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.json")
    try:
        temporary.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()


def load_identity_registry(path: str | os.PathLike[str]) -> IdentityRegistry:
    try:
        record = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read identity registry: {error}") from None
    if not isinstance(record, Mapping) or set(record) != {
        "kind", "schema_version", "registry", "registry_id"
    }:
        raise ValueError("identity registry file fields are invalid")
    if record["kind"] != "fantasy_trade_identity_registry" or record["schema_version"] != 1:
        raise ValueError("identity registry kind or schema version is invalid")
    raw = record["registry"]
    if not isinstance(raw, Mapping):
        raise ValueError("identity registry payload must be a JSON object")
    registry = IdentityRegistry.from_record(raw)
    if record["registry_id"] != _registry_id(registry):
        raise ValueError("identity registry content does not match registry_id")
    return registry


def _record(registry):
    return {
        "kind": "fantasy_trade_identity_registry",
        "schema_version": 1,
        "registry": registry.to_record(),
        "registry_id": _registry_id(registry),
    }


def _registry_id(registry):
    return content_id("identity-registry", registry.to_record())


def _reject_constant(value):
    raise ValueError(f"identity registry contains non-finite JSON constant {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"identity registry contains duplicate JSON key {key!r}")
        result[key] = value
    return result
