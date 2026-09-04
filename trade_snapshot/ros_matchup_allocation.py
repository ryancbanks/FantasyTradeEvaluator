"""Leakage-safe local matchup weights for allocating provider ROS residuals."""

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain
from math import fsum, isfinite
from types import MappingProxyType
from typing import ClassVar, Mapping

from .positions import normalize_player_position
from .public_player_data import (
    DataAvailability,
    PlayerWeekStats,
    SeasonPlayerStats,
)


ROS_MATCHUP_ALLOCATION_METHOD_ID = (
    "nflverse-position-opponent-game-shrunk-v1"
)
ROS_MATCHUP_ALLOCATION_PROVENANCE = (
    "Local position-versus-opponent game totals from eligible completed nflverse "
    "weekly statistics in the requested current/prior inputs; cells require four "
    "games and eight position games, shrink toward neutral by four games, and are "
    "capped at 0.5 through 1.5."
)
ROS_MATCHUP_ALLOCATION_LIMITATION = (
    "Residual rest-of-season points and raw stat components are matchup-weighted "
    "across missing active NFL weeks by local position/opponent factors. Those "
    "weekly shapes are local allocations, not provider-published matchup "
    "projections; target-season weeks at or after the snapshot are excluded, sparse "
    "or unavailable samples and non-QB/RB/WR/TE/K positions use a neutral allocation, "
    "nflverse standard/PPR "
    "points (with Half PPR as their midpoint) may differ from exact league scoring, "
    "every raw-stat residual inherits the fantasy-point share, and corrected "
    "historical nflverse releases can change a later rebuild."
)

_MIN_POSITION_GAMES = 8
_MIN_MATCHUP_GAMES = 4
_NEUTRAL_PRIOR_GAMES = 4
_MIN_FACTOR = 0.5
_MAX_FACTOR = 1.5
_WEIGHTED_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K"})
_TEAM_ALIASES = {"JAC": "JAX", "LA": "LAR", "WAS": "WSH"}


@dataclass(frozen=True, slots=True)
class PositionOpponentWeight:
    """One adequately sampled, shrunk position/opponent factor."""

    position: str
    opponent_team_id: str
    factor: float
    sample_count: int

    def __post_init__(self) -> None:
        position = normalize_player_position(self.position, require_supported=True)
        if position not in _WEIGHTED_POSITIONS:
            raise ValueError("position is not eligible for matchup weighting")
        opponent = _team_id("opponent_team_id", self.opponent_team_id)
        if isinstance(self.factor, bool) or not isinstance(self.factor, (int, float)):
            raise ValueError("factor must be a finite positive number")
        factor = float(self.factor)
        if not isfinite(factor) or factor <= 0:
            raise ValueError("factor must be a finite positive number")
        if (
            type(self.sample_count) is not int
            or self.sample_count < _MIN_MATCHUP_GAMES
        ):
            raise ValueError(
                f"sample_count must be at least {_MIN_MATCHUP_GAMES}"
            )
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "opponent_team_id", opponent)
        object.__setattr__(self, "factor", factor)


@dataclass(frozen=True, slots=True)
class RosMatchupAllocation:
    """Narrow immutable input used only to shape an already-known ROS total."""

    season: int
    as_of_week: int
    scoring: str
    scoring_profile_id: str
    source_data_id: str | None
    weights: tuple[PositionOpponentWeight, ...] = ()
    source_seasons: tuple[int, ...] = ()
    _by_position_opponent: Mapping[tuple[str, str], float] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    method_id: ClassVar[str] = ROS_MATCHUP_ALLOCATION_METHOD_ID
    limitation: ClassVar[str] = ROS_MATCHUP_ALLOCATION_LIMITATION

    def __post_init__(self) -> None:
        _integer("season", self.season, minimum=2012, maximum=9999)
        _integer("as_of_week", self.as_of_week, minimum=1, maximum=25)
        scoring = _scoring(self.scoring)
        _text("scoring_profile_id", self.scoring_profile_id)
        if self.source_data_id is not None:
            _text("source_data_id", self.source_data_id)
        try:
            weights = tuple(self.weights)
        except TypeError:
            raise ValueError("weights must be an iterable") from None
        if any(not isinstance(row, PositionOpponentWeight) for row in weights):
            raise ValueError("weights must contain PositionOpponentWeight values")
        keys = tuple((row.position, row.opponent_team_id) for row in weights)
        if len(set(keys)) != len(keys):
            raise ValueError("weights contain a duplicate position/opponent")
        if self.source_data_id is None and weights:
            raise ValueError("weights require an observed public-data source")
        try:
            source_seasons = tuple(self.source_seasons)
        except TypeError:
            raise ValueError("source_seasons must be an iterable") from None
        if (
            len(set(source_seasons)) != len(source_seasons)
            or any(type(value) is not int for value in source_seasons)
            or not set(source_seasons) <= {self.season - 1, self.season}
        ):
            raise ValueError(
                "source_seasons must contain unique contributing current/prior seasons"
            )
        if self.source_data_id is None and source_seasons:
            raise ValueError("source seasons require an observed public-data source")
        if weights and not source_seasons:
            raise ValueError("weights require at least one contributing source season")
        ordered = tuple(
            sorted(weights, key=lambda row: (row.position, row.opponent_team_id))
        )
        object.__setattr__(self, "scoring", scoring)
        object.__setattr__(self, "weights", ordered)
        object.__setattr__(self, "source_seasons", tuple(sorted(source_seasons)))
        object.__setattr__(
            self,
            "_by_position_opponent",
            MappingProxyType(
                {
                    (row.position, row.opponent_team_id): row.factor
                    for row in ordered
                }
            ),
        )

    @property
    def provenance(self) -> str:
        if self.source_data_id is None:
            return (
                "Public nflverse weekly statistics were unavailable; the local "
                "ROS residual uses neutral allocation."
            )
        if not self.source_seasons:
            return (
                "The public player-data snapshot contained no eligible completed "
                "current/prior nflverse weekly statistics; the local ROS residual "
                "uses neutral allocation. Source snapshot: "
                f"{self.source_data_id}."
            )
        seasons = ", ".join(str(value) for value in self.source_seasons)
        return (
            f"{ROS_MATCHUP_ALLOCATION_PROVENANCE} Contributing seasons: {seasons}. "
            "Source snapshot: "
            f"{self.source_data_id}."
        )

    def factor(self, position: str, opponent_team_id: str) -> float:
        """Return the local factor, or the deterministic neutral factor 1.0."""

        normalized_position = normalize_player_position(position)
        opponent = _team_id("opponent_team_id", opponent_team_id)
        return self._by_position_opponent.get(
            (normalized_position, opponent),
            1.0,
        )

    def validate_context(
        self,
        *,
        season: int,
        as_of_week: int,
        scoring_profile_id: str,
        scoring: str | None = None,
    ) -> None:
        """Reject accidental reuse for another season, week, or scoring mode."""

        _integer("season", season, minimum=2012, maximum=9999)
        _integer("as_of_week", as_of_week, minimum=1, maximum=25)
        _text("scoring_profile_id", scoring_profile_id)
        expected_scoring = self.scoring if scoring is None else _scoring(scoring)
        if (
            self.season != season
            or self.as_of_week != as_of_week
            or self.scoring_profile_id != scoring_profile_id
            or self.scoring != expected_scoring
        ):
            raise ValueError(
                "ROS matchup allocation season, as-of week, and scoring profile "
                "must match; scoring mode must also match when supplied"
            )


def build_ros_matchup_allocation(
    *,
    season: int,
    as_of_week: int,
    scoring: str,
    scoring_profile_id: str,
    current_stats: SeasonPlayerStats,
    previous_stats: SeasonPlayerStats,
    source_data_id: str,
) -> RosMatchupAllocation:
    """Build weights without reading or retaining any target-season future row."""

    _integer("season", season, minimum=2012, maximum=9999)
    _integer("as_of_week", as_of_week, minimum=1, maximum=25)
    scoring = _scoring(scoring)
    _text("scoring_profile_id", scoring_profile_id)
    if not isinstance(current_stats, SeasonPlayerStats):
        raise ValueError("current_stats must be SeasonPlayerStats")
    if current_stats.season != season:
        raise ValueError("current_stats season does not match target season")
    if not isinstance(previous_stats, SeasonPlayerStats):
        raise ValueError("previous_stats must be SeasonPlayerStats")
    if previous_stats.season != season - 1:
        raise ValueError("previous_stats season must immediately precede target season")
    _text("source_data_id", source_data_id)

    rows = chain(
        _observed_rows(previous_stats),
        (
            row
            for row in _observed_rows(current_stats)
            if row.week < as_of_week
        ),
    )
    game_values: dict[tuple[str, str, int, int, str], list[float]] = defaultdict(list)
    source_seasons = set()
    for row in rows:
        position = _weighted_position(row.position)
        points = _fantasy_points(row, scoring)
        if position is None or points is None:
            continue
        source_seasons.add(row.season)
        game_values[
            (
                position,
                row.opponent_team_id,
                row.season,
                row.week,
                row.game_id,
            )
        ].append(points)

    by_position: dict[str, list[float]] = defaultdict(list)
    by_matchup: dict[tuple[str, str], list[float]] = defaultdict(list)
    for key in sorted(game_values):
        position, opponent, _season, _week, _game_id = key
        total = fsum(game_values[key])
        by_position[position].append(total)
        by_matchup[(position, opponent)].append(total)

    weights = []
    for (position, opponent), values in sorted(by_matchup.items()):
        position_values = by_position[position]
        if (
            len(position_values) < _MIN_POSITION_GAMES
            or len(values) < _MIN_MATCHUP_GAMES
        ):
            continue
        baseline = fsum(position_values) / len(position_values)
        matchup_average = fsum(values) / len(values)
        if baseline <= 0 or matchup_average <= 0:
            continue
        raw_factor = matchup_average / baseline
        shrunk_factor = 1.0 + (raw_factor - 1.0) * (
            len(values) / (len(values) + _NEUTRAL_PRIOR_GAMES)
        )
        factor = min(_MAX_FACTOR, max(_MIN_FACTOR, shrunk_factor))
        weights.append(
            PositionOpponentWeight(position, opponent, factor, len(values))
        )

    return RosMatchupAllocation(
        season=season,
        as_of_week=as_of_week,
        scoring=scoring,
        scoring_profile_id=scoring_profile_id,
        source_data_id=source_data_id,
        weights=tuple(weights),
        source_seasons=tuple(source_seasons),
    )


def neutral_ros_matchup_allocation(
    *, season: int, as_of_week: int, scoring: str, scoring_profile_id: str
) -> RosMatchupAllocation:
    """Create an explicit neutral fallback when public weekly stats are unavailable."""

    return RosMatchupAllocation(
        season=season,
        as_of_week=as_of_week,
        scoring=scoring,
        scoring_profile_id=scoring_profile_id,
        source_data_id=None,
    )


def _observed_rows(stats: SeasonPlayerStats) -> tuple[PlayerWeekStats, ...]:
    if stats.availability is not DataAvailability.OBSERVED:
        return ()
    return stats.rows


def _weighted_position(value: str) -> str | None:
    try:
        position = normalize_player_position(value, require_supported=True)
    except ValueError:
        return None
    return position if position in _WEIGHTED_POSITIONS else None


def _fantasy_points(row: PlayerWeekStats, scoring: str) -> float | None:
    if scoring == "STD":
        return row.fantasy_points_standard
    if scoring == "PPR":
        return row.fantasy_points_ppr
    standard = row.fantasy_points_standard
    ppr = row.fantasy_points_ppr
    if standard is None or ppr is None:
        return None
    return (standard + ppr) / 2.0


def _scoring(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("scoring must be STD, HALF, or PPR")
    normalized = value.strip().upper()
    if normalized not in {"STD", "HALF", "PPR"}:
        raise ValueError("scoring must be STD, HALF, or PPR")
    return normalized


def _team_id(name: str, value: object) -> str:
    _text(name, value)
    normalized = value.strip().upper()
    return _TEAM_ALIASES.get(normalized, normalized)


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _integer(name: str, value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


__all__ = (
    "PositionOpponentWeight",
    "ROS_MATCHUP_ALLOCATION_LIMITATION",
    "ROS_MATCHUP_ALLOCATION_METHOD_ID",
    "ROS_MATCHUP_ALLOCATION_PROVENANCE",
    "RosMatchupAllocation",
    "build_ros_matchup_allocation",
    "neutral_ros_matchup_allocation",
)
