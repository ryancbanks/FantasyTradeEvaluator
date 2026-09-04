"""Deterministic aggregation of remaining-season team score scenarios."""

from dataclasses import dataclass
from decimal import Decimal
import math
from numbers import Real
from typing import Iterable

from .league_state import LeagueState, TeamId, TeamStanding
from ._season_ranking import (
    UnresolvedTieError,
    UnsupportedTiebreakerError,
    clone_records,
    new_records,
    prepared_score_rounder,
    rank_teams,
    select_playoff_seeds,
    settle_remaining_matchups,
    validate_tiebreaker_inputs,
)


@dataclass(frozen=True)
class TeamWeekScore:
    team_id: TeamId
    week: int
    score: float

    def __post_init__(self) -> None:
        _require_text("score team_id", self.team_id)
        if isinstance(self.week, bool) or not isinstance(self.week, int) or self.week < 1:
            raise ValueError("score week must be a positive integer")
        try:
            is_finite = math.isfinite(self.score)
        except (TypeError, OverflowError):
            is_finite = False
        if isinstance(self.score, bool) or not isinstance(self.score, Real) or not is_finite:
            raise ValueError("score must be a finite number")


@dataclass(frozen=True)
class ScoreScenario:
    """One complete, equally weighted outcome for all remaining team-weeks."""

    scenario_id: str
    snapshot_id: str
    scoring_profile_id: str
    scores: tuple[TeamWeekScore, ...]

    def __post_init__(self) -> None:
        _require_text("scenario_id", self.scenario_id)
        _require_text("snapshot_id", self.snapshot_id)
        _require_text("scoring_profile_id", self.scoring_profile_id)
        scores = tuple(self.scores)
        if any(not isinstance(score, TeamWeekScore) for score in scores):
            raise ValueError("scores must contain TeamWeekScore values")
        keys = tuple((score.team_id, score.week) for score in scores)
        if len(set(keys)) != len(keys):
            raise ValueError("scenario cannot contain duplicate team-week scores")
        object.__setattr__(self, "scores", scores)


@dataclass(frozen=True)
class TeamSeasonProjection:
    """Aggregate outcome for one team; distribution index zero means rank/seed 1."""

    team_id: TeamId
    current_standing: TeamStanding
    current_rank: int
    expected_final_wins: float
    expected_final_losses: float
    expected_final_ties: float
    expected_final_points_for: float
    expected_final_points_against: float
    mean_rank: float
    rank_distribution: tuple[float, ...]
    seed_distribution: tuple[float, ...]
    playoff_probability: float


@dataclass(frozen=True)
class SeasonProjection:
    snapshot_id: str
    scoring_profile_id: str
    scenario_count: int
    score_decimal_places: int
    random_seed: int
    teams: tuple[TeamSeasonProjection, ...]
    scenario_run_id: str | None = None


def project_remaining_season(
    state: LeagueState,
    scenarios: Iterable[ScoreScenario],
    *,
    score_decimal_places: int = 2,
    random_seed: int = 0,
) -> SeasonProjection:
    """Settle and aggregate equally weighted scenarios.

    Scores are rounded half-up, then captured matchup score adjustments are
    applied before settlement. ``RANDOM_DRAW`` uses a stable draw derived from
    ``random_seed``, ``scenario_id``, and league team order, so scenario ordering
    does not affect results. Division winners are
    resolved within their own division, then occupy the first seeds in
    overall-rank order; wildcards follow. If there are fewer guaranteed berths
    than divisions, the highest-ranked division winners receive them.
    ``POINTS_AGAINST`` ranks the team facing more
    points first. History-based rules require complete results that reproduce
    current standings; head-to-head uses the explicitly captured group policy.
    """

    _validate_options(score_decimal_places, random_seed)
    scenario_run_id = getattr(scenarios, "run_id", None)
    if scenario_run_id is not None and (
        not isinstance(scenario_run_id, str) or not scenario_run_id.strip()
    ):
        raise ValueError("scenario run_id must be non-empty text when provided")
    uses_matchup_history = validate_tiebreaker_inputs(state)
    team_ids = tuple(team.team_id for team in state.teams)
    standings = {standing.team_id: standing for standing in state.standings}
    current_records = new_records(standings)
    current_order = rank_teams(
        current_records,
        state,
        random_seed,
        "$current-standings",
        state.completed_matchups if uses_matchup_history else (),
    )
    current_ranks = {team_id: rank for rank, team_id in enumerate(current_order, 1)}

    totals = {
        team_id: {
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "points_for": Decimal(0),
            "points_against": Decimal(0),
            "rank": 0,
            "rank_counts": [0] * len(team_ids),
            "seed_counts": [0] * state.playoff_rules.qualifier_count,
            "playoffs": 0,
        }
        for team_id in team_ids
    }
    quantum = Decimal(1).scaleb(-score_decimal_places)
    score_rounder = prepared_score_rounder(quantum)
    expected_scores = {
        (team_id, week)
        for team_id in team_ids
        for week in state.remaining_regular_season_weeks
    }
    seen_scenario_ids: set[str] = set()
    count = 0

    for scenario in scenarios:
        score_map = _validate_scenario(
            state, scenario, expected_scores, seen_scenario_ids, score_rounder
        )
        count += 1
        records = clone_records(current_records)
        simulated = settle_remaining_matchups(
            state, records, score_map, uses_matchup_history
        )
        played_games = (
            (*state.completed_matchups, *simulated)
            if uses_matchup_history
            else ()
        )
        order = rank_teams(
            records,
            state,
            random_seed,
            scenario.scenario_id,
            played_games,
        )
        seeds = select_playoff_seeds(
            state,
            order,
            records,
            random_seed,
            scenario.scenario_id,
            played_games,
        )

        for rank, team_id in enumerate(order, 1):
            record = records[team_id]
            total = totals[team_id]
            total["wins"] += record.wins
            total["losses"] += record.losses
            total["ties"] += record.ties
            total["points_for"] += record.points_for
            total["points_against"] += record.points_against
            total["rank"] += rank
            total["rank_counts"][rank - 1] += 1
        for seed, team_id in enumerate(seeds, 1):
            totals[team_id]["seed_counts"][seed - 1] += 1
            totals[team_id]["playoffs"] += 1

    if count == 0:
        raise ValueError("at least one score scenario is required")
    qualifications = sum(total["playoffs"] for total in totals.values())
    if qualifications != count * state.playoff_rules.qualifier_count:
        raise AssertionError("playoff probability sum invariant failed")
    projections = tuple(
        _aggregate_team(
            team_id,
            standings[team_id],
            current_ranks[team_id],
            totals[team_id],
            count,
        )
        for team_id in team_ids
    )
    return SeasonProjection(
        snapshot_id=state.snapshot_id,
        scoring_profile_id=state.scoring_profile_id,
        scenario_count=count,
        score_decimal_places=score_decimal_places,
        random_seed=random_seed,
        teams=projections,
        scenario_run_id=scenario_run_id,
    )


def _validate_options(decimal_places: int, random_seed: int) -> None:
    if (
        isinstance(decimal_places, bool)
        or not isinstance(decimal_places, int)
        or not 0 <= decimal_places <= 9
    ):
        raise ValueError("score_decimal_places must be an integer from 0 through 9")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise ValueError("random_seed must be an integer")


def _validate_scenario(
    state: LeagueState,
    scenario: ScoreScenario,
    expected: set[tuple[TeamId, int]],
    seen_ids: set[str],
    score_rounder,
) -> dict[tuple[TeamId, int], Decimal]:
    if not isinstance(scenario, ScoreScenario):
        raise ValueError("scenarios must contain ScoreScenario values")
    if scenario.scenario_id in seen_ids:
        raise ValueError("scenario_id values must be unique")
    seen_ids.add(scenario.scenario_id)
    if scenario.snapshot_id != state.snapshot_id:
        raise ValueError(f"scenario {scenario.scenario_id!r} has a different snapshot_id")
    if scenario.scoring_profile_id != state.scoring_profile_id:
        raise ValueError(
            f"scenario {scenario.scenario_id!r} has a different scoring_profile_id"
        )
    actual = {(score.team_id, score.week) for score in scenario.scores}
    if actual != expected:
        raise ValueError(
            f"scenario {scenario.scenario_id!r} must contain exactly one score "
            "for every remaining team-week "
            f"(missing={len(expected - actual)}, extra={len(actual - expected)})"
        )
    return {
        (score.team_id, score.week): score_rounder(score.score)
        for score in scenario.scores
    }


def _aggregate_team(
    team_id: TeamId,
    current: TeamStanding,
    current_rank: int,
    total: dict,
    scenario_count: int,
) -> TeamSeasonProjection:
    probability = lambda value: value / scenario_count
    expected_points_for = float(total["points_for"] / scenario_count)
    expected_points_against = float(total["points_against"] / scenario_count)
    if not math.isfinite(expected_points_for) or not math.isfinite(expected_points_against):
        raise ValueError("expected points aggregate is too large for a finite result")
    return TeamSeasonProjection(
        team_id=team_id,
        current_standing=current,
        current_rank=current_rank,
        expected_final_wins=probability(total["wins"]),
        expected_final_losses=probability(total["losses"]),
        expected_final_ties=probability(total["ties"]),
        expected_final_points_for=expected_points_for,
        expected_final_points_against=expected_points_against,
        mean_rank=probability(total["rank"]),
        rank_distribution=tuple(probability(value) for value in total["rank_counts"]),
        seed_distribution=tuple(probability(value) for value in total["seed_counts"]),
        playoff_probability=probability(total["playoffs"]),
    )


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
