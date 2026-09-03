"""Paired before/after season projections using common random numbers."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ._scenario_random import content_id
from .ensemble import EnsembleProjection
from .league_state import LeagueState
from .scenario_config import CorrelatedScenarioConfig, PlayerEligibility
from .scenario_score_cache import ScenarioScoreCache, ScenarioScoreCacheBuilder
from .score_scenarios import PreparedScoreScenarios, prepare_score_scenarios
from .season import (
    ScoreScenario,
    SeasonProjection,
    TeamSeasonProjection,
    project_remaining_season,
)
from .trade_space import TeamRoster


@dataclass(frozen=True, slots=True)
class TeamSeasonChange:
    """One team's paired outcome; positive rank delta means a worse mean finish."""

    team_id: str
    before: TeamSeasonProjection
    after: TeamSeasonProjection

    def __post_init__(self) -> None:
        if not isinstance(self.team_id, str) or not self.team_id.strip():
            raise ValueError("team_id must be a non-empty string")
        if not isinstance(self.before, TeamSeasonProjection) or not isinstance(
            self.after, TeamSeasonProjection
        ):
            raise ValueError("before and after must be TeamSeasonProjection values")
        if self.before.team_id != self.team_id or self.after.team_id != self.team_id:
            raise ValueError("team season projections do not match team_id")

    @property
    def playoff_probability_delta(self) -> float:
        return self.after.playoff_probability - self.before.playoff_probability

    @property
    def expected_wins_delta(self) -> float:
        return self.after.expected_final_wins - self.before.expected_final_wins

    @property
    def mean_rank_delta(self) -> float:
        return self.after.mean_rank - self.before.mean_rank


@dataclass(frozen=True, slots=True)
class PairedSeasonProjection:
    """League projection pair generated from the same underlying player draws."""

    before: SeasonProjection
    after: SeasonProjection
    changes: tuple[TeamSeasonChange, ...]
    before_scenario_run_id: str
    after_scenario_run_id: str
    draw_space_id: str
    impact_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.before, SeasonProjection) or not isinstance(
            self.after, SeasonProjection
        ):
            raise ValueError("before and after must be SeasonProjection values")
        if self.before.scenario_count != self.after.scenario_count:
            raise ValueError("paired projections must use the same scenario count")
        before_identity = (
            self.before.snapshot_id,
            self.before.scoring_profile_id,
            self.before.score_decimal_places,
            self.before.random_seed,
        )
        after_identity = (
            self.after.snapshot_id,
            self.after.scoring_profile_id,
            self.after.score_decimal_places,
            self.after.random_seed,
        )
        if before_identity != after_identity:
            raise ValueError("paired projections must use the same league and options")
        changes = tuple(self.changes)
        if any(not isinstance(row, TeamSeasonChange) for row in changes):
            raise ValueError("changes must contain TeamSeasonChange values")
        before_ids = tuple(row.team_id for row in self.before.teams)
        if tuple(row.team_id for row in self.after.teams) != before_ids:
            raise ValueError("paired projections must contain teams in the same order")
        if tuple(row.team_id for row in changes) != before_ids:
            raise ValueError("changes must exactly cover the projected teams in order")
        for name in (
            "before_scenario_run_id",
            "after_scenario_run_id",
            "draw_space_id",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "changes", changes)
        object.__setattr__(
            self,
            "impact_id",
            content_id(
                "impact",
                {
                    "after_scenario_run_id": self.after_scenario_run_id,
                    "before_scenario_run_id": self.before_scenario_run_id,
                    "draw_space_id": self.draw_space_id,
                    "scenario_count": self.before.scenario_count,
                    "score_decimal_places": self.before.score_decimal_places,
                    "tiebreak_random_seed": self.before.random_seed,
                },
            ),
        )

    def for_team(self, team_id: str) -> TeamSeasonChange:
        try:
            return next(row for row in self.changes if row.team_id == team_id)
        except StopIteration:
            raise KeyError(team_id) from None


@dataclass(frozen=True, slots=True, init=False)
class PreparedSeasonBaseline:
    """One reusable pre-trade simulation for many candidate roster changes."""

    state: LeagueState
    scenarios: PreparedScoreScenarios
    season_projection: SeasonProjection
    score_decimal_places: int
    tiebreak_random_seed: int
    _score_cache: ScenarioScoreCache | None = field(
        default=None, compare=False, repr=False
    )

    def __init__(
        self,
        state: LeagueState,
        scenarios: PreparedScoreScenarios,
        season_projection: SeasonProjection,
        score_decimal_places: int,
        tiebreak_random_seed: int,
        score_cache: ScenarioScoreCache | None = None,
    ) -> None:
        if not isinstance(state, LeagueState):
            raise ValueError("state must be a LeagueState")
        if not isinstance(scenarios, PreparedScoreScenarios):
            raise ValueError("scenarios must be PreparedScoreScenarios")
        if not isinstance(season_projection, SeasonProjection):
            raise ValueError("season_projection must be a SeasonProjection")
        if scenarios.state != state:
            raise ValueError("prepared scenarios do not match league state")
        if (
            season_projection.snapshot_id,
            season_projection.scoring_profile_id,
        ) != (state.snapshot_id, state.scoring_profile_id):
            raise ValueError("season projection does not match league state")
        if season_projection.scenario_count != scenarios.config.scenario_count:
            raise ValueError("season projection and scenario count disagree")
        if (
            season_projection.score_decimal_places != score_decimal_places
            or season_projection.random_seed != tiebreak_random_seed
        ):
            raise ValueError("season projection options do not match the baseline")
        if score_cache is not None:
            if not isinstance(score_cache, ScenarioScoreCache):
                raise ValueError("score_cache must be a ScenarioScoreCache")
            score_cache.recomputed_cell_count(scenarios)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "scenarios", scenarios)
        object.__setattr__(self, "season_projection", season_projection)
        object.__setattr__(self, "score_decimal_places", score_decimal_places)
        object.__setattr__(self, "tiebreak_random_seed", tiebreak_random_seed)
        object.__setattr__(self, "_score_cache", score_cache)

    def project(self, after_rosters: Iterable[TeamRoster]) -> PairedSeasonProjection:
        after_roster_rows = tuple(after_rosters)
        if self._score_cache is None:
            after = prepare_score_scenarios(
                self.state,
                after_roster_rows,
                self.scenarios.projections,
                self.scenarios.eligibilities,
                self.scenarios.config,
            )
            _validate_common_draw_space(self.scenarios, after)
            after_projection = project_remaining_season(
                self.state,
                after,
                score_decimal_places=self.score_decimal_places,
                random_seed=self.tiebreak_random_seed,
            )
        else:
            after = self.scenarios.with_rosters(after_roster_rows)
            _validate_common_draw_space(self.scenarios, after)
            recomputed_cell_count, score_scenarios = (
                self._score_cache.prepare_projection(after)
            )
            if recomputed_cell_count == 0:
                after_projection = self.season_projection
            else:
                after_projection = project_remaining_season(
                    self.state,
                    score_scenarios,
                    score_decimal_places=self.score_decimal_places,
                    random_seed=self.tiebreak_random_seed,
                )
        return _paired_projection(
            self.state,
            self.scenarios,
            after,
            self.season_projection,
            after_projection,
        )

    def iter_baseline_scenarios(self) -> Iterator[ScoreScenario]:
        """Replay realized baseline scores without regenerating cached player draws."""

        if self._score_cache is None:
            return iter(self.scenarios)
        return self._score_cache.iter_scenarios(self.scenarios)


def prepare_season_baseline(
    state: LeagueState,
    rosters: Iterable[TeamRoster],
    projections: Iterable[EnsembleProjection],
    eligibilities: Iterable[PlayerEligibility],
    scenario_config: CorrelatedScenarioConfig,
    *,
    score_decimal_places: int = 2,
    tiebreak_random_seed: int = 0,
) -> PreparedSeasonBaseline:
    scenarios = prepare_score_scenarios(
        state,
        tuple(rosters),
        tuple(projections),
        tuple(eligibilities),
        scenario_config,
    )
    cache_builder = ScenarioScoreCacheBuilder.for_prepared(scenarios)
    projection = project_remaining_season(
        state,
        scenarios if cache_builder is None else cache_builder,
        score_decimal_places=score_decimal_places,
        random_seed=tiebreak_random_seed,
    )
    score_cache = None if cache_builder is None else cache_builder.finish()
    return PreparedSeasonBaseline(
        state,
        scenarios,
        projection,
        score_decimal_places,
        tiebreak_random_seed,
        score_cache,
    )


def project_roster_change(
    state: LeagueState,
    before_rosters: Iterable[TeamRoster],
    after_rosters: Iterable[TeamRoster],
    projections: Iterable[EnsembleProjection],
    eligibilities: Iterable[PlayerEligibility],
    scenario_config: CorrelatedScenarioConfig,
    *,
    score_decimal_places: int = 2,
    tiebreak_random_seed: int = 0,
) -> PairedSeasonProjection:
    """Project a pure roster transfer with identical exogenous player outcomes."""

    before_roster_rows = tuple(before_rosters)
    after_roster_rows = tuple(after_rosters)
    baseline = prepare_season_baseline(
        state,
        before_roster_rows,
        projections,
        eligibilities,
        scenario_config,
        score_decimal_places=score_decimal_places,
        tiebreak_random_seed=tiebreak_random_seed,
    )
    return baseline.project(after_roster_rows)


def _paired_projection(state, before, after, before_projection, after_projection):
    before_by_team = {row.team_id: row for row in before_projection.teams}
    after_by_team = {row.team_id: row for row in after_projection.teams}
    changes = tuple(
        TeamSeasonChange(team.team_id, before_by_team[team.team_id], after_by_team[team.team_id])
        for team in state.teams
    )
    return PairedSeasonProjection(
        before=before_projection,
        after=after_projection,
        changes=changes,
        before_scenario_run_id=before.run_id,
        after_scenario_run_id=after.run_id,
        draw_space_id=before.draw_space_id,
    )


def _validate_common_draw_space(
    before: PreparedScoreScenarios, after: PreparedScoreScenarios
) -> None:
    if before.draw_space_id != after.draw_space_id:
        raise ValueError("paired roster projection could not establish common random draws")
