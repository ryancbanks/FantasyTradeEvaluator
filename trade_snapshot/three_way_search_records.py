"""Strict immutable records for resumable three-team trade searches."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from numbers import Real

from ._search_store_records import (
    _canonical_json,
    _freeze_json,
    _reject_secret_keys,
    _thaw_json,
)
from .three_way_trade import TradeTransfer


THREE_WAY_RUN_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ThreeWayTeamResult:
    team_id: str
    sent_player_ids: tuple[str, ...]
    received_player_ids: tuple[str, ...]
    added_player_ids: tuple[str, ...]
    dropped_player_ids: tuple[str, ...]
    raw_power_delta: float
    display_power_delta: float
    playoff_before: float
    playoff_after: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _nonempty("team_id", self.team_id))
        required = {"sent_player_ids", "received_player_ids"}
        for name in (
            "sent_player_ids",
            "received_player_ids",
            "added_player_ids",
            "dropped_player_ids",
        ):
            object.__setattr__(
                self,
                name,
                _player_ids(name, getattr(self, name), required=name in required),
            )
        if set(self.added_player_ids).intersection(self.dropped_player_ids):
            raise ValueError("a team cannot add and drop the same player")
        for name in ("raw_power_delta", "display_power_delta"):
            object.__setattr__(self, name, _finite(name, getattr(self, name)))
        for name in ("playoff_before", "playoff_after"):
            value = _finite(name, getattr(self, name))
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
            object.__setattr__(self, name, value)

    @property
    def playoff_delta(self) -> float:
        return self.playoff_after - self.playoff_before

    def to_record(self) -> dict[str, object]:
        return {
            "added_player_ids": list(self.added_player_ids),
            "display_power_delta": self.display_power_delta,
            "dropped_player_ids": list(self.dropped_player_ids),
            "playoff_after": self.playoff_after,
            "playoff_before": self.playoff_before,
            "raw_power_delta": self.raw_power_delta,
            "received_player_ids": list(self.received_player_ids),
            "sent_player_ids": list(self.sent_player_ids),
            "team_id": self.team_id,
        }

    @classmethod
    def from_record(cls, record: object) -> "ThreeWayTeamResult":
        fields = {
            "added_player_ids",
            "display_power_delta",
            "dropped_player_ids",
            "playoff_after",
            "playoff_before",
            "raw_power_delta",
            "received_player_ids",
            "sent_player_ids",
            "team_id",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("three-way team result fields are invalid")
        return cls(**{name: record[name] for name in fields})


@dataclass(frozen=True, slots=True)
class ThreeWayQualifiedResult:
    candidate_index: int
    transfers: tuple[TradeTransfer, ...]
    team_results: tuple[ThreeWayTeamResult, ...]

    def __post_init__(self) -> None:
        index = _nonnegative_integer("candidate_index", self.candidate_index)
        transfers = tuple(self.transfers)
        results = tuple(self.team_results)
        if not transfers or any(not isinstance(row, TradeTransfer) for row in transfers):
            raise ValueError("transfers must contain TradeTransfer values")
        if len(results) != 3 or any(
            not isinstance(row, ThreeWayTeamResult) for row in results
        ):
            raise ValueError("team_results must contain exactly three team results")
        team_ids = tuple(row.team_id for row in results)
        if len(set(team_ids)) != 3:
            raise ValueError("team_results must contain three different teams")
        if any(
            leg.source_team_id not in team_ids or leg.destination_team_id not in team_ids
            for leg in transfers
        ):
            raise ValueError("transfers must stay within the three result teams")
        routes = tuple(
            (leg.source_team_id, leg.destination_team_id) for leg in transfers
        )
        moved = tuple(player for leg in transfers for player in leg.player_ids)
        if len(set(routes)) != len(routes) or len(set(moved)) != len(moved):
            raise ValueError("transfer routes and moved players must be unique")
        additions = tuple(
            player_id for result in results for player_id in result.added_player_ids
        )
        drops = tuple(
            player_id for result in results for player_id in result.dropped_player_ids
        )
        if len(set(additions)) != len(additions):
            raise ValueError("one free agent cannot be added by multiple teams")
        if len(set(drops)) != len(drops):
            raise ValueError("one player cannot be dropped by multiple teams")
        adjusted = set(additions).union(drops)
        if set(additions).intersection(drops):
            raise ValueError("a player cannot be both added and dropped")
        if set(moved).intersection(adjusted):
            raise ValueError("a transferred player cannot be added or dropped")
        for result in results:
            sent = tuple(
                player_id
                for leg in transfers
                if leg.source_team_id == result.team_id
                for player_id in leg.player_ids
            )
            received = tuple(
                player_id
                for leg in transfers
                if leg.destination_team_id == result.team_id
                for player_id in leg.player_ids
            )
            if sent != result.sent_player_ids or received != result.received_player_ids:
                raise ValueError("team result packages do not match the transfer legs")
        object.__setattr__(self, "candidate_index", index)
        object.__setattr__(self, "transfers", transfers)
        object.__setattr__(self, "team_results", results)

    @property
    def all_teams_gain(self) -> bool:
        return all(row.playoff_after > row.playoff_before for row in self.team_results)

    @property
    def combined_playoff_delta(self) -> float:
        return sum(row.playoff_delta for row in self.team_results)

    def for_team(self, team_id: str) -> ThreeWayTeamResult:
        try:
            return next(row for row in self.team_results if row.team_id == team_id)
        except StopIteration:
            raise KeyError(team_id) from None

    def to_record(self) -> dict[str, object]:
        return {
            "candidate_index": str(self.candidate_index),
            "team_results": [row.to_record() for row in self.team_results],
            "transfers": [_transfer_record(row) for row in self.transfers],
        }

    @classmethod
    def from_record(cls, record: object) -> "ThreeWayQualifiedResult":
        fields = {"candidate_index", "team_results", "transfers"}
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("three-way qualified result fields are invalid")
        transfers, results = record["transfers"], record["team_results"]
        if not isinstance(transfers, list) or not isinstance(results, list):
            raise ValueError("three-way result collections must be JSON arrays")
        return cls(
            _decimal_integer("candidate_index", record["candidate_index"]),
            tuple(_transfer_from_record(row) for row in transfers),
            tuple(ThreeWayTeamResult.from_record(row) for row in results),
        )


@dataclass(frozen=True, slots=True)
class ThreeWaySearchProgress:
    run_id: str
    next_candidate_index: int
    total_candidate_count: int
    power_qualified_count: int
    playoff_evaluated_count: int
    all_playoff_gain_count: int
    cancelled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _nonempty("run_id", self.run_id))
        for name in (
            "next_candidate_index",
            "total_candidate_count",
            "power_qualified_count",
            "playoff_evaluated_count",
            "all_playoff_gain_count",
        ):
            object.__setattr__(
                self, name, _nonnegative_integer(name, getattr(self, name))
            )
        if self.next_candidate_index > self.total_candidate_count:
            raise ValueError("next_candidate_index cannot exceed total_candidate_count")
        if self.playoff_evaluated_count > self.power_qualified_count:
            raise ValueError("playoff evaluations cannot exceed power qualifiers")
        if self.all_playoff_gain_count > self.playoff_evaluated_count:
            raise ValueError("all-team gains cannot exceed playoff evaluations")
        if not isinstance(self.cancelled, bool):
            raise ValueError("cancelled must be a boolean")

    @property
    def completion_fraction(self) -> float:
        if self.total_candidate_count == 0:
            return 1.0
        return self.next_candidate_index / self.total_candidate_count


@dataclass(frozen=True, slots=True)
class ThreeWaySearchRunDefinition:
    snapshot_id: str
    strength_model_id: str
    participant_team_ids: tuple[str, str, str]
    trade_constraint_record: Mapping[str, object]
    total_candidate_count: int
    schema_version: int = THREE_WAY_RUN_SCHEMA_VERSION
    run_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != THREE_WAY_RUN_SCHEMA_VERSION
        ):
            raise ValueError(f"schema_version must be {THREE_WAY_RUN_SCHEMA_VERSION}")
        object.__setattr__(self, "snapshot_id", _nonempty("snapshot_id", self.snapshot_id))
        object.__setattr__(
            self,
            "strength_model_id",
            _nonempty("strength_model_id", self.strength_model_id),
        )
        participants = tuple(self.participant_team_ids)
        if len(participants) != 3 or any(
            not isinstance(row, str) or not row for row in participants
        ):
            raise ValueError("participant_team_ids must contain three team IDs")
        if len(set(participants)) != 3:
            raise ValueError("participant_team_ids contains a duplicate")
        if not isinstance(self.trade_constraint_record, Mapping):
            raise ValueError("trade_constraint_record must be a JSON object")
        constraints = _freeze_json(self.trade_constraint_record)
        _reject_secret_keys(constraints)
        object.__setattr__(self, "participant_team_ids", participants)
        object.__setattr__(self, "trade_constraint_record", constraints)
        object.__setattr__(
            self,
            "total_candidate_count",
            _nonnegative_integer("total_candidate_count", self.total_candidate_count),
        )
        digest = sha256(_canonical_json(self._identity_record()).encode("utf-8")).hexdigest()
        object.__setattr__(self, "run_id", f"three-way-search-run-v1-{digest}")

    def _identity_record(self) -> dict[str, object]:
        return {
            "participant_team_ids": list(self.participant_team_ids),
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "strength_model_id": self.strength_model_id,
            "total_candidate_count": str(self.total_candidate_count),
            "trade_constraint_record": _thaw_json(self.trade_constraint_record),
        }

    def to_record(self) -> dict[str, object]:
        return {**self._identity_record(), "run_id": self.run_id}

    @classmethod
    def from_record(cls, record: object) -> "ThreeWaySearchRunDefinition":
        fields = {
            "participant_team_ids",
            "run_id",
            "schema_version",
            "snapshot_id",
            "strength_model_id",
            "total_candidate_count",
            "trade_constraint_record",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("three-way run definition fields are invalid")
        participants = record["participant_team_ids"]
        if not isinstance(participants, list):
            raise ValueError("participant_team_ids must be a JSON array")
        definition = cls(
            snapshot_id=record["snapshot_id"],
            strength_model_id=record["strength_model_id"],
            participant_team_ids=tuple(participants),
            trade_constraint_record=record["trade_constraint_record"],
            total_candidate_count=_decimal_integer(
                "total_candidate_count", record["total_candidate_count"]
            ),
            schema_version=record["schema_version"],
        )
        if record["run_id"] != definition.run_id:
            raise ValueError("three-way run definition does not match run_id")
        return definition


def _transfer_record(transfer: TradeTransfer) -> dict[str, object]:
    return {
        "destination_team_id": transfer.destination_team_id,
        "player_ids": list(transfer.player_ids),
        "source_team_id": transfer.source_team_id,
    }


def _transfer_from_record(record: object) -> TradeTransfer:
    fields = {"destination_team_id", "player_ids", "source_team_id"}
    if not isinstance(record, Mapping) or set(record) != fields:
        raise ValueError("three-way transfer fields are invalid")
    return TradeTransfer(
        record["source_team_id"], record["destination_team_id"], record["player_ids"]
    )


def _decimal_integer(name: str, value: object) -> int:
    if (
        not isinstance(value, str)
        or not value
        or any(character < "0" or character > "9" for character in value)
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise ValueError(f"{name} must be a canonical non-negative decimal string")
    return int(value)


def _nonnegative_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _player_ids(name: str, values: object, *, required: bool) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a collection of player IDs")
    try:
        result = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be a collection of player IDs") from None
    if (required and not result) or any(
        not isinstance(row, str) or not row for row in result
    ):
        raise ValueError(f"{name} must contain non-empty player IDs")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} contains a duplicate player")
    return result


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result
