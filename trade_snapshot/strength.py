"""Whole-roster scoring with immutable calibrated starter/depth roles."""

from dataclasses import dataclass, field
from math import fsum, isfinite
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from .lineup import LineupPlayer, LineupResult, _optimize_prepared_lineup
from .strength_calibration import (
    CalibrationMetadata,
    CalibrationStatus,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
    _content_id,
    _finite_number,
    _nonempty_string,
    _require_exact_fields,
)


_MODEL_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RosterStrength:
    residual_score: float
    assignment_score: float
    absolute_score: float
    power_score: float
    role_assignment: LineupResult


@dataclass(frozen=True, slots=True)
class TeamStrengthChange:
    before: RosterStrength
    after: RosterStrength

    @property
    def absolute_delta(self) -> float:
        return self.after.absolute_score - self.before.absolute_score

    @property
    def power_delta(self) -> float:
        return self.after.power_score - self.before.power_score


@dataclass(frozen=True, slots=True)
class TradeStrengthResult:
    primary: TeamStrengthChange
    counterparty: TeamStrengthChange


@dataclass(frozen=True, slots=True, init=False)
class StrengthModel:
    """A frozen residual plus optimal starter/depth role assignment model."""

    snapshot_id: str
    season: int
    scoring_profile_id: str
    role_definitions: tuple[RoleDefinition, ...]
    players: Mapping[str, PlayerStrength]
    normalization_denominator: float
    calibration: CalibrationMetadata
    model_id: str
    _lineup_players: Mapping[str, LineupPlayer] = field(
        repr=False, compare=False
    )
    _scored_roster_roles: tuple[str, ...] = field(repr=False, compare=False)

    def __init__(
        self,
        role_definitions: Sequence[RoleDefinition],
        players: Iterable[PlayerStrength],
        normalization_denominator: float,
        *,
        snapshot_id: str,
        season: int,
        scoring_profile_id: str,
        calibration: CalibrationMetadata,
    ) -> None:
        snapshot = _nonempty_string("snapshot_id", snapshot_id)
        if isinstance(season, bool) or not isinstance(season, int) or season < 2012:
            raise ValueError("season must be an integer of 2012 or later")
        profile = _nonempty_string("scoring_profile_id", scoring_profile_id)
        roles = _normalized_role_definitions(role_definitions)
        denominator = _finite_number(
            "normalization_denominator",
            normalization_denominator,
        )
        if denominator <= 0:
            raise ValueError("normalization_denominator must be greater than zero")
        if not isinstance(calibration, CalibrationMetadata):
            raise ValueError("calibration must be CalibrationMetadata")

        player_rows = tuple(players)
        if any(not isinstance(player, PlayerStrength) for player in player_rows):
            raise ValueError("players must contain only PlayerStrength values")
        player_map: dict[str, PlayerStrength] = {}
        for player in player_rows:
            if player.player_id in player_map:
                raise ValueError("players contain a duplicate player_id")
            _validate_player_role_coverage(player, roles)
            player_map[player.player_id] = player

        model_record = {
            "calibration_evidence_id": calibration.evidence_id,
            "normalization_denominator": denominator,
            "players": [
                {
                    "assignment_score_by_role": dict(sorted(row.assignment_score_by_role.items())),
                    "eligible_positions": sorted(row.eligible_positions),
                    "player_id": row.player_id,
                    "residual_score": row.residual_score,
                }
                for row in sorted(player_rows, key=lambda value: value.player_id)
            ],
            "roles": [role.to_record() for role in roles],
            "schema_version": _MODEL_SCHEMA_VERSION,
            "scoring_profile_id": profile,
            "season": season,
            "snapshot_id": snapshot,
        }
        object.__setattr__(self, "snapshot_id", snapshot)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "scoring_profile_id", profile)
        object.__setattr__(self, "role_definitions", roles)
        object.__setattr__(self, "players", MappingProxyType(player_map))
        object.__setattr__(self, "normalization_denominator", denominator)
        object.__setattr__(self, "calibration", calibration)
        object.__setattr__(self, "model_id", _content_id("strength", model_record))
        object.__setattr__(
            self,
            "_lineup_players",
            MappingProxyType(
                {
                    player_id: LineupPlayer(
                        player_id, player.assignment_score_by_role
                    )
                    for player_id, player in player_map.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "_scored_roster_roles",
            tuple(role.role_id for role in roles),
        )

    @property
    def scored_roster_roles(self) -> tuple[str, ...]:
        return self._scored_roster_roles

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": _MODEL_SCHEMA_VERSION,
            "model_id": self.model_id,
            "snapshot_id": self.snapshot_id,
            "season": self.season,
            "scoring_profile_id": self.scoring_profile_id,
            "normalization_denominator": self.normalization_denominator,
            "calibration": self.calibration.to_record(),
            "role_definitions": [role.to_record() for role in self.role_definitions],
            "players": [
                self.players[player_id].to_record()
                for player_id in sorted(self.players)
            ],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "StrengthModel":
        _require_exact_fields(
            "strength model",
            record,
            {
                "schema_version",
                "model_id",
                "snapshot_id",
                "season",
                "scoring_profile_id",
                "normalization_denominator",
                "calibration",
                "role_definitions",
                "players",
            },
        )
        if (
            isinstance(record["schema_version"], bool)
            or not isinstance(record["schema_version"], int)
            or record["schema_version"] != _MODEL_SCHEMA_VERSION
        ):
            raise ValueError(f"strength model schema_version must be {_MODEL_SCHEMA_VERSION}")
        calibration_record = record["calibration"]
        if not isinstance(calibration_record, Mapping):
            raise ValueError("calibration must be a mapping")
        role_records = _record_mapping_list("role_definitions", record["role_definitions"])
        player_records = _record_mapping_list("players", record["players"])
        model = cls(
            role_definitions=tuple(
                RoleDefinition.from_record(role_record)
                for role_record in role_records
            ),
            players=tuple(
                PlayerStrength.from_record(player_record)
                for player_record in player_records
            ),
            normalization_denominator=record["normalization_denominator"],
            snapshot_id=record["snapshot_id"],
            season=record["season"],
            scoring_profile_id=record["scoring_profile_id"],
            calibration=CalibrationMetadata.from_record(calibration_record),
        )
        if record["model_id"] != model.model_id:
            raise ValueError("strength model content does not match model_id")
        return model

    def score_roster(self, roster_player_ids: Iterable[str]) -> RosterStrength:
        roster_ids = _unique_ids("roster", roster_player_ids)
        try:
            rows = tuple(self.players[player_id] for player_id in roster_ids)
        except KeyError as error:
            raise ValueError(f"missing strength calibration for player_id {error.args[0]!r}") from None

        residual_score = _finite_sum(
            "roster residual scores",
            (row.residual_score for row in rows),
        )
        role_assignment = _optimize_prepared_lineup(
            self._scored_roster_roles,
            tuple(self._lineup_players[row.player_id] for row in rows),
        )
        absolute_score = _finite_sum(
            "roster strength",
            (residual_score, role_assignment.total_weight),
        )
        power_score = 100.0 * absolute_score / self.normalization_denominator
        if not isfinite(power_score):
            raise ValueError("roster power score is not finite")
        return RosterStrength(
            residual_score=residual_score,
            assignment_score=role_assignment.total_weight,
            absolute_score=absolute_score,
            power_score=power_score,
            role_assignment=role_assignment,
        )

    def evaluate_trade(
        self,
        *,
        primary_roster: Iterable[str],
        counterparty_roster: Iterable[str],
        outgoing_player_ids: Iterable[str],
        incoming_player_ids: Iterable[str],
    ) -> TradeStrengthResult:
        primary_ids = _unique_ids("primary roster", primary_roster)
        counterparty_ids = _unique_ids("counterparty roster", counterparty_roster)
        if set(primary_ids).intersection(counterparty_ids):
            raise ValueError("a player_id cannot be owned by both teams")

        outgoing = _unique_ids("outgoing package", outgoing_player_ids)
        incoming = _unique_ids("incoming package", incoming_player_ids)
        if not outgoing or not incoming:
            raise ValueError("both trade packages must contain at least one player")
        if not set(outgoing).issubset(primary_ids):
            raise ValueError("outgoing package contains a player not on the primary roster")
        if not set(incoming).issubset(counterparty_ids):
            raise ValueError("incoming package contains a player not on the counterparty roster")

        outgoing_set = set(outgoing)
        incoming_set = set(incoming)
        primary_after = tuple(
            player_id for player_id in primary_ids if player_id not in outgoing_set
        ) + incoming
        counterparty_after = tuple(
            player_id for player_id in counterparty_ids if player_id not in incoming_set
        ) + outgoing
        return TradeStrengthResult(
            primary=TeamStrengthChange(
                before=self.score_roster(primary_ids),
                after=self.score_roster(primary_after),
            ),
            counterparty=TeamStrengthChange(
                before=self.score_roster(counterparty_ids),
                after=self.score_roster(counterparty_after),
            ),
        )


def _normalized_role_definitions(
    roles: Sequence[RoleDefinition],
) -> tuple[RoleDefinition, ...]:
    if isinstance(roles, (str, bytes)):
        raise ValueError("role_definitions must be a sequence of RoleDefinition values")
    try:
        normalized = tuple(roles)
    except TypeError:
        raise ValueError("role_definitions must be a sequence of RoleDefinition values") from None
    if not normalized:
        raise ValueError("role_definitions cannot be empty")
    if any(not isinstance(role, RoleDefinition) for role in normalized):
        raise ValueError("role_definitions must contain only RoleDefinition values")
    role_ids = tuple(role.role_id for role in normalized)
    if len(set(role_ids)) != len(role_ids):
        raise ValueError("role_definitions contain a duplicate role_id")
    return normalized


def _validate_player_role_coverage(
    player: PlayerStrength,
    roles: tuple[RoleDefinition, ...],
) -> None:
    model_role_ids = {role.role_id for role in roles}
    expected = {
        role.role_id
        for role in roles
        if player.eligible_positions.intersection(role.eligible_positions)
    }
    actual = set(player.assignment_score_by_role)
    unknown = actual.difference(model_role_ids)
    if unknown:
        raise ValueError(
            f"player {player.player_id!r} has a score for unknown role {min(unknown)!r}"
        )
    if actual != expected:
        missing = expected.difference(actual)
        extra = actual.difference(expected)
        detail = f"missing {sorted(missing)!r}" if missing else f"ineligible {sorted(extra)!r}"
        raise ValueError(
            f"player {player.player_id!r} role calibration is incomplete: {detail}"
        )


def _unique_ids(name: str, values: Iterable[str]) -> tuple[str, ...]:
    try:
        identifiers = tuple(values)
        unique = set(identifiers)
    except TypeError:
        raise ValueError(f"{name} player_ids must be iterable and hashable") from None
    if any(not isinstance(player_id, str) or not player_id for player_id in identifiers):
        raise ValueError(f"{name} player_ids must be non-empty strings")
    if len(unique) != len(identifiers):
        raise ValueError(f"{name} contains a duplicate player_id")
    return identifiers


def _finite_sum(name: str, values: Iterable[float]) -> float:
    try:
        total = fsum(values)
    except OverflowError:
        raise ValueError(f"{name} is not finite") from None
    if not isfinite(total):
        raise ValueError(f"{name} is not finite")
    return total


def _record_mapping_list(
    name: str,
    value: object,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"{name} must be a list of mappings")
    return tuple(value)
