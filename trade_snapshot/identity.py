"""Exact, auditable cross-provider player identity records."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any


__all__ = (
    "IdentityRegistry",
    "ManualMappingProvenance",
    "PlayerIdentity",
    "ProviderReference",
    "UnresolvedProviderRecord",
)

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ManualMappingProvenance:
    """Audit evidence for a provider reference linked by a person."""

    mapped_by: str
    mapped_at: datetime
    evidence: str

    def __post_init__(self) -> None:
        _require_nonempty_string("mapped_by", self.mapped_by)
        _require_aware_datetime("mapped_at", self.mapped_at)
        _require_nonempty_string("evidence", self.evidence)

    def to_record(self) -> dict[str, str]:
        return {
            "mapped_by": self.mapped_by,
            "mapped_at": self.mapped_at.isoformat(timespec="microseconds"),
            "evidence": self.evidence,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ManualMappingProvenance":
        _require_record_fields(
            record,
            {"mapped_by", "mapped_at", "evidence"},
            "manual mapping provenance",
        )
        return cls(
            mapped_by=record["mapped_by"],
            mapped_at=_parse_aware_datetime(record["mapped_at"], "mapped_at"),
            evidence=record["evidence"],
        )


@dataclass(frozen=True, slots=True)
class ProviderReference:
    """One provider's stable player identifier, matched only as an exact pair."""

    provider: str
    provider_player_id: str
    manual_mapping: ManualMappingProvenance | None = None

    def __post_init__(self) -> None:
        _require_nonempty_string("provider", self.provider)
        _require_nonempty_string("provider_player_id", self.provider_player_id)
        if self.manual_mapping is not None and not isinstance(
            self.manual_mapping, ManualMappingProvenance
        ):
            raise ValueError("manual_mapping must be ManualMappingProvenance or None")

    @property
    def key(self) -> tuple[str, str]:
        return self.provider, self.provider_player_id

    def to_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_player_id": self.provider_player_id,
            "manual_mapping": (
                self.manual_mapping.to_record()
                if self.manual_mapping is not None
                else None
            ),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ProviderReference":
        _require_record_fields(
            record,
            {"provider", "provider_player_id", "manual_mapping"},
            "provider reference",
        )
        manual_record = record["manual_mapping"]
        if manual_record is not None and not isinstance(manual_record, Mapping):
            raise ValueError("manual_mapping must be a mapping or null")
        return cls(
            provider=record["provider"],
            provider_player_id=record["provider_player_id"],
            manual_mapping=(
                ManualMappingProvenance.from_record(manual_record)
                if manual_record is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PlayerIdentity:
    """A canonical player and its verified provider identifiers."""

    canonical_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    provider_references: tuple[ProviderReference, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_string("canonical_player_id", self.canonical_player_id)
        _require_player_fields(self.display_name, self.position, self.nfl_team_id)
        references = _typed_tuple(
            "provider_references", self.provider_references, ProviderReference
        )
        keys = [reference.key for reference in references]
        if len(set(keys)) != len(keys):
            raise ValueError("player has a duplicate provider reference")
        object.__setattr__(self, "provider_references", references)

    def to_record(self) -> dict[str, Any]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "display_name": self.display_name,
            "position": self.position,
            "nfl_team_id": self.nfl_team_id,
            "provider_references": [
                reference.to_record() for reference in self.provider_references
            ],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "PlayerIdentity":
        _require_record_fields(
            record,
            {
                "canonical_player_id",
                "display_name",
                "position",
                "nfl_team_id",
                "provider_references",
            },
            "player identity",
        )
        references = _require_record_list(
            record["provider_references"], "provider_references"
        )
        return cls(
            canonical_player_id=record["canonical_player_id"],
            display_name=record["display_name"],
            position=record["position"],
            nfl_team_id=record["nfl_team_id"],
            provider_references=tuple(
                ProviderReference.from_record(reference) for reference in references
            ),
        )


@dataclass(frozen=True, slots=True)
class UnresolvedProviderRecord:
    """A provider row retained explicitly until a verified mapping is supplied."""

    provider_reference: ProviderReference
    display_name: str
    position: str
    nfl_team_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider_reference, ProviderReference):
            raise ValueError("provider_reference must be a ProviderReference")
        if self.provider_reference.manual_mapping is not None:
            raise ValueError(
                "an unresolved provider reference cannot have manual mapping provenance"
            )
        _require_player_fields(self.display_name, self.position, self.nfl_team_id)
        _require_nonempty_string("reason", self.reason)

    def to_record(self) -> dict[str, Any]:
        return {
            "provider_reference": self.provider_reference.to_record(),
            "display_name": self.display_name,
            "position": self.position,
            "nfl_team_id": self.nfl_team_id,
            "reason": self.reason,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "UnresolvedProviderRecord":
        _require_record_fields(
            record,
            {
                "provider_reference",
                "display_name",
                "position",
                "nfl_team_id",
                "reason",
            },
            "unresolved provider record",
        )
        reference = record["provider_reference"]
        if not isinstance(reference, Mapping):
            raise ValueError("provider_reference must be a mapping")
        return cls(
            provider_reference=ProviderReference.from_record(reference),
            display_name=record["display_name"],
            position=record["position"],
            nfl_team_id=record["nfl_team_id"],
            reason=record["reason"],
        )


@dataclass(frozen=True, slots=True)
class IdentityRegistry:
    """Collision-free exact lookup over resolved and explicit unresolved rows."""

    players: tuple[PlayerIdentity, ...]
    unresolved: tuple[UnresolvedProviderRecord, ...] = ()
    _players_by_reference: Mapping[tuple[str, str], PlayerIdentity] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _unresolved_by_reference: Mapping[
        tuple[str, str], UnresolvedProviderRecord
    ] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        players = _typed_tuple("players", self.players, PlayerIdentity)
        unresolved = _typed_tuple(
            "unresolved", self.unresolved, UnresolvedProviderRecord
        )
        canonical_ids = [player.canonical_player_id for player in players]
        if len(set(canonical_ids)) != len(canonical_ids):
            raise ValueError("registry contains a duplicate canonical_player_id")

        resolved_index: dict[tuple[str, str], PlayerIdentity] = {}
        for player in players:
            for reference in player.provider_references:
                if reference.key in resolved_index:
                    raise ValueError(
                        "provider reference is mapped to multiple canonical players"
                    )
                resolved_index[reference.key] = player

        unresolved_index: dict[tuple[str, str], UnresolvedProviderRecord] = {}
        for row in unresolved:
            key = row.provider_reference.key
            if key in resolved_index:
                raise ValueError(
                    "provider reference cannot be both resolved and unresolved"
                )
            if key in unresolved_index:
                raise ValueError(
                    "registry contains a duplicate unresolved provider reference"
                )
            unresolved_index[key] = row

        object.__setattr__(self, "players", players)
        object.__setattr__(self, "unresolved", unresolved)
        object.__setattr__(
            self, "_players_by_reference", MappingProxyType(resolved_index)
        )
        object.__setattr__(
            self, "_unresolved_by_reference", MappingProxyType(unresolved_index)
        )

    def lookup(self, provider: str, provider_player_id: str) -> PlayerIdentity | None:
        """Return only an exact verified mapping; never infer from names or metadata."""

        key = _lookup_key(provider, provider_player_id)
        return self._players_by_reference.get(key)

    def unresolved_for(
        self, provider: str, provider_player_id: str
    ) -> UnresolvedProviderRecord | None:
        """Return an exact unresolved row without treating it as a player match."""

        key = _lookup_key(provider, provider_player_id)
        return self._unresolved_by_reference.get(key)

    def to_record(self) -> dict[str, Any]:
        """Return a detached, JSON-safe registry record."""

        return {
            "schema_version": _SCHEMA_VERSION,
            "players": [player.to_record() for player in self.players],
            "unresolved": [row.to_record() for row in self.unresolved],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "IdentityRegistry":
        """Rebuild a registry while validating every nested record and collision."""

        _require_record_fields(
            record,
            {"schema_version", "players", "unresolved"},
            "identity registry",
        )
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError(f"schema_version must be {_SCHEMA_VERSION}")
        player_records = _require_record_list(record["players"], "players")
        unresolved_records = _require_record_list(record["unresolved"], "unresolved")
        return cls(
            players=tuple(PlayerIdentity.from_record(row) for row in player_records),
            unresolved=tuple(
                UnresolvedProviderRecord.from_record(row) for row in unresolved_records
            ),
        )


def _lookup_key(provider: object, provider_player_id: object) -> tuple[str, str]:
    _require_nonempty_string("provider", provider)
    _require_nonempty_string("provider_player_id", provider_player_id)
    return provider, provider_player_id


def _require_player_fields(
    display_name: object, position: object, team: object
) -> None:
    _require_nonempty_string("display_name", display_name)
    _require_nonempty_string("position", position)
    _require_nonempty_string("nfl_team_id", team)


def _require_nonempty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_aware_datetime(name: str, value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _parse_aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 string") from None
    _require_aware_datetime(name, parsed)
    return parsed


def _require_record_fields(
    record: object, expected: set[str], record_name: str
) -> None:
    if not isinstance(record, Mapping):
        raise ValueError(f"{record_name} must be a mapping")
    if set(record) != expected:
        raise ValueError(f"{record_name} has missing or unknown fields")


def _require_record_list(value: object, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(row, Mapping) for row in value
    ):
        raise ValueError(f"{name} must be a list of mappings")
    return value


def _typed_tuple(name: str, values: Iterable[Any], item_type: type) -> tuple[Any, ...]:
    try:
        copied = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if any(not isinstance(value, item_type) for value in copied):
        raise ValueError(f"{name} must contain only {item_type.__name__} values")
    return copied
