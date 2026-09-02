"""Content-addressed, immutable fantasy scoring profiles."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any


_SCHEMA_VERSION = 1
_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
_RECORD_FIELDS = frozenset(
    {"schema_version", "scoring_profile_id", "platform", "settings"}
)


@dataclass(frozen=True)
class ScoringProfile:
    """Captured scoring settings whose identifier changes with their content.

    Provider adapters remain responsible for proving that every platform field
    required by their versioned schema was captured.
    """

    platform: str
    settings: Mapping[str, Any]
    schema_version: int = _SCHEMA_VERSION
    scoring_profile_id: str = field(init=False)
    canonical_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.platform, str) or not self.platform.strip():
            raise ValueError("platform must be a non-empty string")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != _SCHEMA_VERSION
        ):
            raise ValueError(f"schema_version must be {_SCHEMA_VERSION}")
        if not isinstance(self.settings, Mapping) or not self.settings:
            raise ValueError("settings must be a non-empty mapping")

        normalized_settings = _normalize_json(self.settings, "settings")
        payload = {
            "platform": self.platform,
            "schema_version": self.schema_version,
            "settings": normalized_settings,
        }
        canonical_json = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = sha256(canonical_json.encode("utf-8")).hexdigest()
        object.__setattr__(self, "settings", _freeze_json(normalized_settings))
        object.__setattr__(self, "canonical_json", canonical_json)
        object.__setattr__(
            self,
            "scoring_profile_id",
            f"scoring-v{self.schema_version}-{digest}",
        )

    def to_record(self) -> dict[str, Any]:
        """Return a detached JSON-safe record including the verified identifier."""

        record = json.loads(self.canonical_json)
        record["scoring_profile_id"] = self.scoring_profile_id
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ScoringProfile":
        """Recompute and verify a stored profile instead of trusting its ID."""

        if not isinstance(record, Mapping):
            raise ValueError("scoring profile record must be a mapping")
        if set(record) != _RECORD_FIELDS:
            raise ValueError("scoring profile record has missing or unknown fields")
        expected_id = record["scoring_profile_id"]
        if not isinstance(expected_id, str) or not expected_id:
            raise ValueError("scoring_profile_id must be a non-empty string")
        profile = cls(
            platform=record["platform"],
            settings=record["settings"],
            schema_version=record["schema_version"],
        )
        if profile.scoring_profile_id != expected_id:
            raise ValueError("scoring profile content does not match scoring_profile_id")
        return profile


def _normalize_json(value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            normalized[key] = _normalize_json(child, f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json(child, f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{path} integer is outside the portable JSON range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain a non-finite number")
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{path} number is outside the portable JSON range")
        if value.is_integer():
            return int(value)
        return value
    raise ValueError(f"{path} contains a value that is not JSON-compatible")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value
