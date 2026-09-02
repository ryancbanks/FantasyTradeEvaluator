"""Strict immutable records and JSON helpers for resumable trade searches."""

from dataclasses import dataclass, field
from hashlib import sha256
import json
from math import isfinite
from numbers import Real
from types import MappingProxyType
from typing import Mapping


SEARCH_RUN_SCHEMA_VERSION = 1
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_RUN_INPUT_FIELDS = (
    "snapshot_id",
    "strength_model_id",
    "primary_team_id",
    "counterparty_team_id",
    "trade_constraint_record",
    "total_candidate_count",
    "schema_version",
)
_POWER_FIELDS = (
    "primary_raw_power_delta",
    "primary_display_power_delta",
    "counterparty_raw_power_delta",
    "counterparty_display_power_delta",
)
_ODDS_FIELDS = (
    "primary_playoff_before",
    "primary_playoff_after",
    "counterparty_playoff_before",
    "counterparty_playoff_after",
)
_ADJUSTMENT_FIELDS = (
    "primary_added_player_ids",
    "primary_dropped_player_ids",
    "counterparty_added_player_ids",
    "counterparty_dropped_player_ids",
)


@dataclass(frozen=True, slots=True)
class SearchRunDefinition:
    snapshot_id: str
    strength_model_id: str
    primary_team_id: str
    counterparty_team_id: str
    trade_constraint_record: Mapping[str, object]
    total_candidate_count: int
    schema_version: int = SEARCH_RUN_SCHEMA_VERSION
    run_id: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != SEARCH_RUN_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SEARCH_RUN_SCHEMA_VERSION}")
        for name in _RUN_INPUT_FIELDS[:4]:
            object.__setattr__(self, name, _nonempty(name, getattr(self, name)))
        if self.primary_team_id == self.counterparty_team_id:
            raise ValueError("primary and counterparty team IDs must be different")
        count = _sqlite_integer("total_candidate_count", self.total_candidate_count)
        if not isinstance(self.trade_constraint_record, Mapping):
            raise ValueError("trade_constraint_record must be a JSON object")
        constraints = _freeze_json(self.trade_constraint_record)
        _reject_secret_keys(constraints)
        object.__setattr__(self, "trade_constraint_record", constraints)
        object.__setattr__(self, "total_candidate_count", count)
        digest = sha256(_canonical_json(self._identity_record()).encode("utf-8")).hexdigest()
        object.__setattr__(self, "run_id", f"search-run-v1-{digest}")

    def _identity_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "strength_model_id": self.strength_model_id,
            "primary_team_id": self.primary_team_id,
            "counterparty_team_id": self.counterparty_team_id,
            "trade_constraint_record": _thaw_json(self.trade_constraint_record),
            "total_candidate_count": self.total_candidate_count,
        }

    def to_record(self) -> dict[str, object]:
        return {**self._identity_record(), "run_id": self.run_id}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "SearchRunDefinition":
        expected = set(_RUN_INPUT_FIELDS) | {"run_id"}
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ValueError("search run definition fields do not match the schema")
        definition = cls(**{name: record[name] for name in _RUN_INPUT_FIELDS})
        if record["run_id"] != definition.run_id:
            raise ValueError("search run definition does not match run_id")
        return definition


@dataclass(frozen=True, slots=True)
class QualifiedSearchResult:
    candidate_index: int
    outgoing_player_ids: tuple[str, ...]
    incoming_player_ids: tuple[str, ...]
    primary_raw_power_delta: float
    primary_display_power_delta: float
    counterparty_raw_power_delta: float
    counterparty_display_power_delta: float
    primary_added_player_ids: tuple[str, ...] = ()
    primary_dropped_player_ids: tuple[str, ...] = ()
    counterparty_added_player_ids: tuple[str, ...] = ()
    counterparty_dropped_player_ids: tuple[str, ...] = ()
    primary_playoff_before: float | None = None
    primary_playoff_after: float | None = None
    counterparty_playoff_before: float | None = None
    counterparty_playoff_after: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_index", _sqlite_integer("candidate_index", self.candidate_index)
        )
        outgoing = _package("outgoing_player_ids", self.outgoing_player_ids)
        incoming = _package("incoming_player_ids", self.incoming_player_ids)
        if set(outgoing).intersection(incoming):
            raise ValueError("outgoing and incoming packages cannot share a player_id")
        object.__setattr__(self, "outgoing_player_ids", outgoing)
        object.__setattr__(self, "incoming_player_ids", incoming)
        for name in _ADJUSTMENT_FIELDS:
            object.__setattr__(self, name, _optional_package(name, getattr(self, name)))
        if set(self.primary_added_player_ids).intersection(self.primary_dropped_player_ids):
            raise ValueError("primary adjustments cannot add and drop the same player")
        if set(self.counterparty_added_player_ids).intersection(
            self.counterparty_dropped_player_ids
        ):
            raise ValueError("counterparty adjustments cannot add and drop the same player")
        for name in _POWER_FIELDS:
            object.__setattr__(self, name, _finite(name, getattr(self, name)))
        odds = tuple(getattr(self, name) for name in _ODDS_FIELDS)
        if any(value is None for value in odds) and not all(value is None for value in odds):
            raise ValueError("playoff odds must provide before and after values for both teams")
        if odds[0] is not None:
            for name, value in zip(_ODDS_FIELDS, odds):
                normalized = _finite(name, value)
                if not 0 <= normalized <= 100:
                    raise ValueError(f"{name} must be between 0 and 100")
                object.__setattr__(self, name, normalized)


@dataclass(frozen=True, slots=True)
class SearchResumeState:
    next_candidate_index: int
    qualified_results: tuple[QualifiedSearchResult, ...]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _strict_json_loads(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("stored JSON must be text")

    def reject_constant(constant: str):
        raise ValueError(f"non-finite JSON constant {constant}")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = child
        return result

    return json.loads(
        value, parse_constant=reject_constant, object_pairs_hook=reject_duplicate_keys
    )


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("trade_constraint_record must contain finite JSON values")
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("trade_constraint_record JSON object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(child) for key, child in value.items()}
        )
    raise ValueError("trade_constraint_record must contain only strict JSON values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _reject_secret_keys(value: object) -> None:
    forbidden = {
        "accesstoken", "apikey", "authorization", "clientsecret", "cookie", "key",
        "password", "refreshtoken", "secret", "sessionid", "token",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(
                character for character in key.casefold() if character.isalnum()
            )
            if normalized in forbidden:
                raise ValueError(
                    f"trade_constraint_record contains secret-like key {key!r}"
                )
            _reject_secret_keys(child)
    elif isinstance(value, tuple):
        for child in value:
            _reject_secret_keys(child)


def _package(name: str, values: object) -> tuple[str, ...]:
    normalized = _optional_package(name, values)
    if not normalized:
        raise ValueError(f"{name} must contain non-empty string player IDs")
    return normalized


def _optional_package(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a collection of player IDs")
    try:
        normalized = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be a collection of player IDs") from None
    if any(not isinstance(value, str) or not value for value in normalized):
        raise ValueError(f"{name} must contain non-empty string player IDs")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} contains a duplicate player_id")
    return normalized


def _nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _sqlite_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    if value > _MAX_SQLITE_INTEGER:
        raise ValueError(f"{name} exceeds SQLite's supported integer range")
    return value


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(normalized):
        raise ValueError(f"{name} must be a finite number")
    return normalized
