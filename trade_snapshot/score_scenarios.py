"""Streaming, correlated team-score scenarios from weekly ensembles."""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from math import fsum, isfinite
from types import MappingProxyType

from ._scenario_random import (
    DRAW_ALGORITHM,
    content_id,
    require_json_int,
    require_text,
    standard_normal,
)
from .ensemble import EnsembleProjection, ensemble_to_record
from .league_state import LeagueState
from .lineup import LineupPlayer, optimize_lineup
from .projections import ProjectionStatus
from .scenario_config import CorrelatedScenarioConfig, FactorLoadings, PlayerEligibility
from .season import ScoreScenario, TeamWeekScore
from .trade_space import TeamRoster


__all__ = (
    "CorrelatedScenarioConfig", "FactorLoadings", "PlayerEligibility",
    "PreparedScoreScenarios", "prepare_score_scenarios",
)

@dataclass(frozen=True, slots=True)
class PreparedScoreScenarios:
    """Validated inputs plus lazy deterministic scenario generation.

    Factor loadings preserve each projection's variance before the fixed
    zero-point floor; that floor can reduce variance for projections near zero.
    """

    state: LeagueState
    rosters: tuple[TeamRoster, ...]
    projections: tuple[EnsembleProjection, ...]
    eligibilities: tuple[PlayerEligibility, ...]
    config: CorrelatedScenarioConfig
    projection_set_id: str = field(init=False)
    draw_space_id: str = field(init=False)
    run_id: str = field(init=False)
    _projection_by_key: Mapping[tuple[str, int], EnsembleProjection] = field(
        init=False, repr=False, compare=False
    )
    _lineups: Mapping[tuple[str, int], tuple[str, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.state, LeagueState):
            raise ValueError("state must be a LeagueState")
        if not isinstance(self.config, CorrelatedScenarioConfig):
            raise ValueError("config must be a CorrelatedScenarioConfig")
        rosters = _validated_rosters(self.state, self.rosters)
        rostered_player_ids = frozenset(
            player for roster in rosters for player in roster.player_ids
        )
        eligibilities = _validated_eligibilities(
            self.state, self.eligibilities, rostered_player_ids
        )
        player_ids = frozenset(row.canonical_player_id for row in eligibilities)
        projections, by_key = _validated_projections(
            self.state, self.projections, player_ids, self.config.loadings
        )
        projection_records = [ensemble_to_record(item) for item in projections]
        projection_set_id = content_id("sproj", {"projections": projection_records})
        draw_space_id = content_id(
            "sdraw",
            {
                "algorithm": DRAW_ALGORITHM,
                "loadings": self.config.loadings.to_record(),
                "projection_set_id": projection_set_id,
                "scoring_profile_id": self.state.scoring_profile_id,
                "season": self.state.season,
                "seed": self.config.seed,
                "snapshot_id": self.state.snapshot_id,
            },
        )
        lineups = _select_lineups(self.state, rosters, eligibilities, by_key)
        run_id = content_id(
            "srun",
            _run_record(
                self.state,
                rosters,
                eligibilities,
                self.config,
                projection_set_id,
                draw_space_id,
            ),
        )
        object.__setattr__(self, "rosters", rosters)
        object.__setattr__(self, "projections", projections)
        object.__setattr__(self, "eligibilities", eligibilities)
        object.__setattr__(self, "projection_set_id", projection_set_id)
        object.__setattr__(self, "draw_space_id", draw_space_id)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "_projection_by_key", MappingProxyType(by_key))
        object.__setattr__(self, "_lineups", MappingProxyType(lineups))

    def __iter__(self) -> Iterator[ScoreScenario]:
        return self.iter_scenarios()

    def iter_scenarios(
        self, start: int = 0, stop: int | None = None
    ) -> Iterator[ScoreScenario]:
        """Yield a resumable slice without retaining completed scenarios."""

        end = self.config.scenario_count if stop is None else stop
        require_json_int("start", start, minimum=0)
        require_json_int("stop", end, minimum=start)
        if end > self.config.scenario_count:
            raise ValueError("stop cannot exceed scenario_count")
        for index in range(start, end):
            yield self._scenario(index)

    def player_score(
        self, canonical_player_id: str, week: int, scenario_index: int
    ) -> float:
        """Realize one player using keys independent of fantasy-roster ownership."""

        require_text("canonical_player_id", canonical_player_id)
        require_json_int("week", week, minimum=1)
        require_json_int("scenario_index", scenario_index, minimum=0)
        if scenario_index >= self.config.scenario_count:
            raise ValueError("scenario_index must be less than scenario_count")
        try:
            projection = self._projection_by_key[(canonical_player_id, week)]
        except KeyError:
            raise ValueError("player/week is outside this scenario run") from None
        if projection.status is ProjectionStatus.BYE:
            return 0.0
        return _realize(
            projection,
            self.config.loadings,
            self.draw_space_id,
            scenario_index,
            {},
        )

    def identity_record(self) -> dict[str, object]:
        record = _run_record(
            self.state,
            self.rosters,
            self.eligibilities,
            self.config,
            self.projection_set_id,
            self.draw_space_id,
        )
        return {
            "kind": "prepared_score_scenarios",
            "schema_version": 3,
            **record,
            "run_id": self.run_id,
        }

    def _scenario(self, index: int) -> ScoreScenario:
        scores = []
        draw_cache: dict[tuple[str, tuple[object, ...]], float] = {}
        for week in self.state.remaining_regular_season_weeks:
            for team_id in sorted(team.team_id for team in self.state.teams):
                player_ids = self._lineups[(team_id, week)]
                total = fsum(
                    _realize(
                        self._projection_by_key[(player_id, week)],
                        self.config.loadings,
                        self.draw_space_id,
                        index,
                        draw_cache,
                    )
                    for player_id in player_ids
                )
                scores.append(TeamWeekScore(team_id, week, total))
        return ScoreScenario(
            scenario_id=f"{self.draw_space_id}:{index}",
            snapshot_id=self.state.snapshot_id,
            scoring_profile_id=self.state.scoring_profile_id,
            scores=tuple(scores),
        )


def prepare_score_scenarios(
    state: LeagueState,
    rosters: Iterable[TeamRoster],
    projections: Iterable[EnsembleProjection],
    eligibilities: Iterable[PlayerEligibility],
    config: CorrelatedScenarioConfig,
) -> PreparedScoreScenarios:
    """Validate once, select mean-optimal lineups, and return a lazy stream."""

    return PreparedScoreScenarios(
        state, tuple(rosters), tuple(projections), tuple(eligibilities), config
    )


def _validated_rosters(
    state: LeagueState, values: Iterable[TeamRoster]
) -> tuple[TeamRoster, ...]:
    try:
        rosters = tuple(values)
    except TypeError:
        raise ValueError("rosters must be an iterable") from None
    if any(not isinstance(item, TeamRoster) for item in rosters):
        raise ValueError("rosters must contain TeamRoster values")
    if len({item.team_id for item in rosters}) != len(rosters):
        raise ValueError("rosters contain a duplicate team_id")
    if {item.team_id for item in rosters} != {item.team_id for item in state.teams}:
        raise ValueError("rosters must contain every league team exactly once")
    seen: set[str] = set()
    normalized = []
    for roster in rosters:
        if roster.current_size != len(roster.player_ids):
            raise ValueError("scenario generation requires full rosters")
        if roster.roster_cap != state.roster_rules.roster_cap:
            raise ValueError("roster cap does not match league rules")
        if seen.intersection(roster.player_ids):
            raise ValueError("a player cannot be owned by multiple teams")
        seen.update(roster.player_ids)
        normalized.append(
            TeamRoster(
                roster.team_id,
                tuple(sorted(roster.player_ids)),
                roster.current_size,
                roster.roster_cap,
                roster.capacity_exempt_player_ids,
            )
        )
    return tuple(sorted(normalized, key=lambda item: item.team_id))


def _validated_eligibilities(
    state: LeagueState,
    values: Iterable[PlayerEligibility],
    player_ids: frozenset[str],
) -> tuple[PlayerEligibility, ...]:
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("eligibilities must be an iterable") from None
    if any(not isinstance(row, PlayerEligibility) for row in rows):
        raise ValueError("eligibilities must contain PlayerEligibility values")
    by_player = {row.canonical_player_id: row for row in rows}
    if len(by_player) != len(rows):
        raise ValueError("eligibilities contain a duplicate player")
    missing = set(player_ids).difference(by_player)
    if missing:
        raise ValueError("eligibilities must contain every rostered player")
    allowed = set(state.roster_rules.starting_lineup_slots)
    for row in rows:
        unknown = set(row.eligible_slots).difference(allowed)
        if unknown:
            raise ValueError(
                f"player eligibility contains unknown lineup slot {min(unknown)!r}"
            )
    return tuple(sorted(rows, key=lambda row: row.canonical_player_id))


def _validated_projections(
    state: LeagueState,
    values: Iterable[EnsembleProjection],
    player_ids: frozenset[str],
    loadings: FactorLoadings,
) -> tuple[
    tuple[EnsembleProjection, ...],
    dict[tuple[str, int], EnsembleProjection],
]:
    try:
        projections = tuple(values)
    except TypeError:
        raise ValueError("projections must be an iterable") from None
    if any(not isinstance(row, EnsembleProjection) for row in projections):
        raise ValueError("projections must contain EnsembleProjection values")
    by_key: dict[tuple[str, int], EnsembleProjection] = {}
    expected_identity = (state.snapshot_id, state.scoring_profile_id, state.season)
    for row in projections:
        if (row.snapshot_id, row.scoring_profile_id, row.season) != expected_identity:
            raise ValueError("ensemble projection identity does not match league state")
        key = (row.canonical_player_id, row.week)
        if key in by_key:
            raise ValueError("duplicate ensemble player/week projection")
        if row.status is ProjectionStatus.OBSERVED:
            if loadings.game and row.nfl_game_id is None:
                raise ValueError("game loading requires NFL game IDs for observed projections")
            if loadings.nfl_team and row.nfl_team_id is None:
                raise ValueError("NFL-team loading requires NFL team IDs for observed projections")
        by_key[key] = row
    expected = {
        (player, week)
        for player in player_ids
        for week in state.remaining_regular_season_weeks
    }
    if set(by_key) != expected:
        raise ValueError("projections must contain every rostered player/week exactly once")
    return tuple(sorted(projections, key=lambda row: (row.canonical_player_id, row.week))), by_key


def _select_lineups(state, rosters, eligibilities, projections):
    eligibility = {row.canonical_player_id: row.eligible_slots for row in eligibilities}
    selected: dict[tuple[str, int], tuple[str, ...]] = {}
    for roster in rosters:
        for week in state.remaining_regular_season_weeks:
            players = []
            for player_id in roster.player_ids:
                projection = projections[(player_id, week)]
                if projection.status is ProjectionStatus.BYE:
                    continue
                weights = {
                    slot: projection.projected_fantasy_points
                    for slot in eligibility[player_id]
                }
                players.append(LineupPlayer(player_id, weights))
            result = optimize_lineup(state.roster_rules.starting_lineup_slots, players)
            selected[(roster.team_id, week)] = tuple(
                assignment.player_id
                for assignment in result.assignments
                if assignment.player_id is not None
            )
    return selected


def _realize(projection, loadings, draw_space_id, scenario_index, cache):
    if projection.predictive_stddev == 0:
        return max(0.0, projection.projected_fantasy_points)
    week = projection.week
    factors = (
        (loadings.league, "league", (week,)),
        (loadings.game, "game", (week, projection.nfl_game_id)),
        (loadings.nfl_team, "nfl_team", (week, projection.nfl_team_id)),
        (loadings.player, "player", (week, projection.canonical_player_id)),
    )
    shock = fsum(
        loading * _cached_normal(cache, draw_space_id, scenario_index, component, parts)
        for loading, component, parts in factors
        if loading
    )
    result = max(
        0.0,
        projection.projected_fantasy_points + projection.predictive_stddev * shock,
    )
    if not isfinite(result):
        raise ValueError("realized player score is not finite")
    return result


def _cached_normal(cache, draw_space_id, scenario_index, component, parts):
    key = (component, parts)
    value = cache.get(key)
    if value is None:
        value = standard_normal(draw_space_id, scenario_index, component, parts)
        cache[key] = value
    return value


def _run_record(state, rosters, eligibilities, config, projection_set_id, draw_space_id):
    return {
        "config_id": config.config_id,
        "draw_space_id": draw_space_id,
        "eligibilities": [
            {
                "canonical_player_id": row.canonical_player_id,
                "eligible_slots": list(row.eligible_slots),
            }
            for row in eligibilities
        ],
        "first_remaining_week": state.first_remaining_week,
        "remaining_weeks": list(state.remaining_regular_season_weeks),
        "remaining_matchups": [
            {
                "team1_id": row.team1_id,
                "team1_score_adjustment": row.team1_score_adjustment,
                "team2_id": row.team2_id,
                "week": row.week,
            }
            for row in sorted(
                state.remaining_matchups,
                key=lambda value: (value.week, value.team1_id, value.team2_id),
            )
        ],
        "projection_set_id": projection_set_id,
        "roster_cap": state.roster_rules.roster_cap,
        "rosters": [
            {
                "capacity_exempt_player_ids": sorted(
                    row.capacity_exempt_player_ids
                ),
                "player_ids": list(row.player_ids),
                "team_id": row.team_id,
            }
            for row in rosters
        ],
        "scoring_profile_id": state.scoring_profile_id,
        "season": state.season,
        "snapshot_id": state.snapshot_id,
        "starting_lineup_slots": list(state.roster_rules.starting_lineup_slots),
    }
