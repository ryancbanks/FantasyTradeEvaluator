"""Immutable inputs and leakage checks for strength calibration."""

from dataclasses import dataclass, field
from math import isfinite
from numbers import Real
from types import MappingProxyType
from typing import Iterable, Mapping

from .roster_capacity import assign_reserve_slots
from .strength_calibration import (
    RoleDefinition,
    _content_id,
    _nonempty_string,
    _normalized_names as _names,
)
from .trade_space import TeamRoster


_MAX_FEATURE_MAGNITUDE = 1e12
_MAX_POWER_SCORE = 1e6


@dataclass(frozen=True, slots=True)
class PlayerFeatureVector:
    player_id: str
    eligible_positions: frozenset[str]
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "player_id", _nonempty_string("player_id", self.player_id))
        positions = _names("eligible_positions", self.eligible_positions)
        if not isinstance(self.values, Mapping) or not self.values:
            raise ValueError("values must be a non-empty mapping")
        normalized: dict[str, float] = {}
        for name, value in self.values.items():
            key = _nonempty_string("feature name", name)
            normalized[key] = _bounded_finite(
                "feature value", value, _MAX_FEATURE_MAGNITUDE
            )
        object.__setattr__(self, "eligible_positions", frozenset(positions))
        object.__setattr__(self, "values", MappingProxyType(dict(sorted(normalized.items()))))


@dataclass(frozen=True, slots=True)
class RosterPowerSample:
    """One training score for a complete, explicitly identified roster."""

    sample_id: str
    team_id: str
    roster_player_ids: tuple[str, ...]
    raw_power_score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _nonempty_string("sample_id", self.sample_id))
        object.__setattr__(self, "team_id", _nonempty_string("team_id", self.team_id))
        object.__setattr__(
            self,
            "roster_player_ids",
            tuple(sorted(_names("roster_player_ids", self.roster_player_ids))),
        )
        object.__setattr__(
            self,
            "raw_power_score",
            _nonnegative_bounded("raw_power_score", self.raw_power_score, _MAX_POWER_SCORE),
        )


@dataclass(frozen=True, slots=True)
class CalibrationTradeObservation:
    """A held-out bilateral trade, including both complete before/after rosters."""

    trade_id: str
    team1_id: str
    team2_id: str
    team1_before_player_ids: tuple[str, ...]
    team1_after_player_ids: tuple[str, ...]
    team2_before_player_ids: tuple[str, ...]
    team2_after_player_ids: tuple[str, ...]
    team1_raw_before: float
    team1_raw_after: float
    team2_raw_before: float
    team2_raw_after: float
    observation_id: str = field(init=False)
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        trade_id = _nonempty_string("trade_id", self.trade_id)
        team1 = _nonempty_string("team1_id", self.team1_id)
        team2 = _nonempty_string("team2_id", self.team2_id)
        if team1 == team2:
            raise ValueError("held-out trade teams must be different")
        roster_names = (
            "team1_before_player_ids",
            "team1_after_player_ids",
            "team2_before_player_ids",
            "team2_after_player_ids",
        )
        rosters = {
            name: tuple(sorted(_names(name, getattr(self, name)))) for name in roster_names
        }
        before1, after1, before2, after2 = (rosters[name] for name in roster_names)
        _validate_transfer(before1, after1, before2, after2)
        scores = {}
        for name in (
            "team1_raw_before",
            "team1_raw_after",
            "team2_raw_before",
            "team2_raw_after",
        ):
            scores[name] = _nonnegative_bounded(name, getattr(self, name), _MAX_POWER_SCORE)
        for name, roster in rosters.items():
            object.__setattr__(self, name, roster)
        for name, score in scores.items():
            object.__setattr__(self, name, score)
        object.__setattr__(self, "trade_id", trade_id)
        object.__setattr__(self, "team1_id", team1)
        object.__setattr__(self, "team2_id", team2)
        team_rows = sorted(
            (
                (team1, before1, after1, scores["team1_raw_before"], scores["team1_raw_after"]),
                (team2, before2, after2, scores["team2_raw_before"], scores["team2_raw_after"]),
            )
        )
        object.__setattr__(
            self,
            "observation_id",
            _content_id(
                "heldout-trade",
                {
                    "teams": [
                        {
                            "after": list(after),
                            "before": list(before),
                            "team_id": team,
                        }
                        for team, before, after, _, _ in team_rows
                    ]
                },
            ),
        )
        object.__setattr__(
            self,
            "evidence_id",
            _content_id(
                "heldout-evidence",
                {
                    "observation_id": self.observation_id,
                    "scores": [
                        {
                            "raw_after": raw_after,
                            "raw_before": raw_before,
                            "team_id": team,
                        }
                        for team, _, _, raw_before, raw_after in team_rows
                    ],
                },
            ),
        )


@dataclass(frozen=True, slots=True, init=False)
class CalibrationCorpus:
    snapshot_id: str
    season: int
    scoring_profile_id: str
    role_definitions: tuple[RoleDefinition, ...]
    player_features: tuple[PlayerFeatureVector, ...]
    baseline_rosters: Mapping[str, tuple[str, ...]]
    roster_cap: int
    reserve_slot_counts: Mapping[str, int]
    reserve_slot_by_player: Mapping[str, str]
    samples: tuple[RosterPowerSample, ...]
    held_out_trades: tuple[CalibrationTradeObservation, ...]
    corpus_id: str

    def __init__(
        self,
        *,
        snapshot_id: str,
        season: int,
        scoring_profile_id: str,
        role_definitions: Iterable[RoleDefinition],
        player_features: Iterable[PlayerFeatureVector],
        baseline_rosters: Iterable[TeamRoster],
        samples: Iterable[RosterPowerSample],
        held_out_trades: Iterable[CalibrationTradeObservation] = (),
    ) -> None:
        snapshot = _nonempty_string("snapshot_id", snapshot_id)
        profile = _nonempty_string("scoring_profile_id", scoring_profile_id)
        if type(season) is not int or season < 2012:
            raise ValueError("season must be an integer of 2012 or later")
        roles = tuple(role_definitions)
        if not roles or any(not isinstance(row, RoleDefinition) for row in roles):
            raise ValueError("role_definitions must contain RoleDefinition values")
        if len({row.role_id for row in roles}) != len(roles):
            raise ValueError("role_definitions contain a duplicate role_id")
        features = tuple(player_features)
        if not features or any(not isinstance(row, PlayerFeatureVector) for row in features):
            raise ValueError("player_features must contain PlayerFeatureVector values")
        feature_by_id = {row.player_id: row for row in features}
        if len(feature_by_id) != len(features):
            raise ValueError("player_features contain a duplicate player_id")
        feature_names = tuple(features[0].values)
        if any(tuple(row.values) != feature_names for row in features):
            raise ValueError("every player must have exactly the same feature names")
        (
            baselines,
            roster_cap,
            reserve_slot_counts,
            reserve_slot_by_player,
        ) = _baselines(baseline_rosters, feature_by_id)
        sample_rows = tuple(samples)
        heldouts = tuple(held_out_trades)
        _validate_samples(
            sample_rows,
            heldouts,
            baselines,
            roster_cap,
            reserve_slot_counts,
            reserve_slot_by_player,
            feature_by_id,
        )
        sample_rows = tuple(sorted(sample_rows, key=lambda row: row.sample_id))
        heldouts = tuple(sorted(heldouts, key=lambda row: row.trade_id))
        record = _corpus_record(
            snapshot,
            season,
            profile,
            roles,
            features,
            baselines,
            roster_cap,
            reserve_slot_counts,
            reserve_slot_by_player,
            sample_rows,
            heldouts,
        )
        object.__setattr__(self, "snapshot_id", snapshot)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "scoring_profile_id", profile)
        object.__setattr__(self, "role_definitions", roles)
        object.__setattr__(
            self,
            "player_features",
            tuple(sorted(features, key=lambda row: row.player_id)),
        )
        object.__setattr__(self, "baseline_rosters", MappingProxyType(baselines))
        object.__setattr__(self, "roster_cap", roster_cap)
        object.__setattr__(
            self,
            "reserve_slot_counts",
            MappingProxyType(dict(reserve_slot_counts)),
        )
        object.__setattr__(
            self,
            "reserve_slot_by_player",
            MappingProxyType(dict(reserve_slot_by_player)),
        )
        object.__setattr__(self, "samples", sample_rows)
        object.__setattr__(self, "held_out_trades", heldouts)
        object.__setattr__(self, "corpus_id", _content_id("calibration-corpus", record))


def _baselines(values, features):
    rows = tuple(values)
    if len(rows) < 2 or any(not isinstance(row, TeamRoster) for row in rows):
        raise ValueError("baseline_rosters must contain at least two TeamRoster values")
    if len({row.team_id for row in rows}) != len(rows):
        raise ValueError("baseline_rosters contain a duplicate team_id")
    caps = {row.roster_cap for row in rows}
    if len(caps) != 1:
        raise ValueError("baseline_rosters must use one league-wide roster cap")
    reserve_capacity_signatures = {
        tuple(row.reserve_slot_counts.items()) for row in rows
    }
    if len(reserve_capacity_signatures) != 1:
        raise ValueError(
            "baseline_rosters must use one league-wide set of reserve-slot capacities"
        )
    result, reserve_slots, owned = {}, {}, set()
    for row in sorted(rows, key=lambda item: item.team_id):
        if row.current_size != len(row.player_ids):
            raise ValueError("each baseline TeamRoster must contain its complete current roster")
        unknown = set(row.player_ids).difference(features)
        if unknown:
            raise ValueError(f"baseline roster references unknown player {min(unknown)!r}")
        overlap = owned.intersection(row.player_ids)
        if overlap:
            raise ValueError(f"baseline rosters share player {min(overlap)!r}")
        owned.update(row.player_ids)
        result[row.team_id] = tuple(sorted(row.player_ids))
        reserve_slots.update(row.reserve_slot_by_player)
    reserve_slot_counts = dict(next(iter(reserve_capacity_signatures)))
    return result, next(iter(caps)), reserve_slot_counts, reserve_slots


def _validate_samples(
    samples,
    heldouts,
    baselines,
    cap,
    reserve_slot_counts,
    reserve_slot_by_player,
    features,
):
    if not samples or any(not isinstance(row, RosterPowerSample) for row in samples):
        raise ValueError("samples must contain RosterPowerSample values")
    if len({row.sample_id for row in samples}) != len(samples):
        raise ValueError("samples contain a duplicate sample_id")
    semantic_samples = {row.roster_player_ids for row in samples}
    if len(semantic_samples) != len(samples):
        raise ValueError("samples contain the same semantic team roster more than once")
    for row in samples:
        _validate_roster(
            row.team_id,
            row.roster_player_ids,
            baselines,
            cap,
            reserve_slot_counts,
            reserve_slot_by_player,
            features,
        )
    baseline_scores = {}
    for team_id, roster in baselines.items():
        anchors = [
            row for row in samples
            if row.team_id == team_id and row.roster_player_ids == roster
        ]
        if len(anchors) != 1:
            raise ValueError("each team needs exactly one training sample for its baseline roster")
        baseline_scores[team_id] = anchors[0].raw_power_score
    if any(not isinstance(row, CalibrationTradeObservation) for row in heldouts):
        raise ValueError("held_out_trades must contain CalibrationTradeObservation values")
    if len({row.trade_id for row in heldouts}) != len(heldouts):
        raise ValueError("held_out_trades contain a duplicate trade_id")
    if len({row.observation_id for row in heldouts}) != len(heldouts):
        raise ValueError("held_out_trades contain a repeated semantic trade")
    for trade in heldouts:
        for team_id, before, after, raw_before in (
            (
                trade.team1_id,
                trade.team1_before_player_ids,
                trade.team1_after_player_ids,
                trade.team1_raw_before,
            ),
            (
                trade.team2_id,
                trade.team2_before_player_ids,
                trade.team2_after_player_ids,
                trade.team2_raw_before,
            ),
        ):
            _validate_roster(
                team_id,
                after,
                baselines,
                cap,
                reserve_slot_counts,
                reserve_slot_by_player,
                features,
            )
            if before != baselines[team_id]:
                raise ValueError("held-out trade before roster must equal the captured baseline")
            if raw_before != baseline_scores[team_id]:
                raise ValueError("held-out raw-before score must equal the baseline anchor")
            if after in semantic_samples:
                raise ValueError("held-out trade after roster leaks into training samples")


def _validate_roster(
    team_id,
    roster,
    baselines,
    cap,
    reserve_slot_counts,
    reserve_slot_by_player,
    features,
):
    if team_id not in baselines:
        raise ValueError("calibration row team_id is not a baseline league team")
    unknown = set(roster).difference(features)
    if unknown:
        raise ValueError(f"calibration roster references unknown player {min(unknown)!r}")
    active_occupancy = _active_occupancy(
        roster, reserve_slot_counts, reserve_slot_by_player
    )
    if active_occupancy > cap:
        raise ValueError("calibration roster exceeds the captured active roster cap")
    baseline_active_occupancy = _active_occupancy(
        baselines[team_id], reserve_slot_counts, reserve_slot_by_player
    )
    if active_occupancy != baseline_active_occupancy:
        raise ValueError(
            "calibration roster changes the team's captured active roster occupancy"
        )


def _active_occupancy(roster, reserve_slot_counts, reserve_slot_by_player):
    reserve_candidates = {
        player_id: reserve_slot_by_player[player_id]
        for player_id in roster
        if player_id in reserve_slot_by_player
    }
    assigned = assign_reserve_slots(
        reserve_candidates,
        reserve_slot_counts,
        roster,
    )
    return len(roster) - len(assigned)


def _validate_transfer(before1, after1, before2, after2):
    if set(before1).intersection(before2) or set(after1).intersection(after2):
        raise ValueError("a trade player cannot belong to both teams")
    if set(before1).union(before2) != set(after1).union(after2):
        raise ValueError("held-out trade must conserve the complete player set")
    if before1 == after1 or before2 == after2:
        raise ValueError("held-out trade must change both team rosters")


def _corpus_record(
    snapshot,
    season,
    profile,
    roles,
    features,
    baselines,
    cap,
    reserve_slot_counts,
    reserve_slot_by_player,
    samples,
    heldouts,
):
    return {
        "baseline_rosters": {key: list(value) for key, value in baselines.items()},
        "held_out_trades": [row.evidence_id for row in heldouts],
        "player_features": [
            {
                "eligible_positions": sorted(row.eligible_positions),
                "player_id": row.player_id,
                "values": dict(row.values),
            }
            for row in sorted(features, key=lambda item: item.player_id)
        ],
        "role_definitions": [row.to_record() for row in roles],
        "roster_cap": cap,
        "reserve_slot_by_player": dict(reserve_slot_by_player),
        "reserve_slot_counts": dict(reserve_slot_counts),
        "samples": [
            {
                "raw_power_score": row.raw_power_score,
                "roster_player_ids": list(row.roster_player_ids),
                "sample_id": row.sample_id,
                "team_id": row.team_id,
            }
            for row in samples
        ],
        "schema_version": 3,
        "scoring_profile_id": profile,
        "season": season,
        "snapshot_id": snapshot,
    }


def _bounded_finite(name: str, value: object, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(result) or abs(result) > maximum:
        raise ValueError(f"{name} is outside the supported numeric range")
    return result


def _nonnegative_bounded(name: str, value: object, maximum: float) -> float:
    result = _bounded_finite(name, value, maximum)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result
