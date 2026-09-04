"""Deterministic, JSON-ready league-dashboard analysis.

The dashboard deliberately reuses the weekly engine's score scenarios and
regular-season ranking rules.  Championship probability is a disclosed proxy:
the bundle has no postseason player projections or bracket outcomes to simulate.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from math import fsum, sqrt

from ._dashboard_validation import (
    validate_dashboard_inputs,
    validate_dashboard_result,
)
from ._season_ranking import (
    _add_score_adjustment,
    clone_records,
    new_records,
    prepared_score_rounder,
    rank_teams,
    select_playoff_seeds,
    settle_remaining_matchups,
    validate_tiebreaker_inputs,
)
from .engine_bundle import EngineBundle
from .score_scenarios import PreparedScoreScenarios
from .season import ScoreScenario, SeasonProjection


_SCHEMA_VERSION = 1
_TITLE_POWER_POINTS_PER_DOUBLING = 10.0
_POSITION_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "K", "DST")


@dataclass(slots=True)
class _Moments:
    count: int = 0
    mean: float = 0.0
    squared_deviation: float = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.squared_deviation += delta * (value - self.mean)

    @property
    def standard_deviation(self) -> float:
        if not self.count:
            raise AssertionError("cannot summarize an empty sample")
        return sqrt(max(0.0, self.squared_deviation / self.count))


def build_league_dashboard(
    bundle: EngineBundle,
    baseline_projection: SeasonProjection,
    scenarios: PreparedScoreScenarios,
    realized_scenarios: Iterable[ScoreScenario] | None = None,
) -> dict[str, object]:
    """Build the complete standalone dashboard contract for one weekly bundle."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    validate_dashboard_inputs(bundle, baseline_projection, scenarios)

    state = bundle.state
    weeks = state.remaining_regular_season_weeks
    team_names = {team.team_id: team.name for team in state.teams}
    roster_by_team = {roster.team_id: roster for roster in bundle.rosters}
    projection_by_team = {
        team.team_id: team for team in baseline_projection.teams
    }
    power_scores = {
        team_id: bundle.strength_model.score_roster(roster.player_ids).power_score
        for team_id, roster in roster_by_team.items()
    }
    power_ranks = _ordinal_ranks(
        team_names,
        lambda team_id: (-power_scores[team_id],),
    )
    projected_ranks = _ordinal_ranks(
        team_names,
        lambda team_id: (
            projection_by_team[team_id].mean_rank,
            -projection_by_team[team_id].expected_final_wins,
        ),
    )

    weekly, win_points, title_totals = _scenario_summaries(
        bundle,
        baseline_projection,
        scenarios if realized_scenarios is None else realized_scenarios,
        power_scores,
    )
    championship = {
        team_id: title_totals[team_id] / baseline_projection.scenario_count
        for team_id in team_names
    }
    weekly_means = {
        key: moments.mean for key, moments in weekly.items()
    }
    opponent_by_team_week = _opponents(state.remaining_matchups)
    average_opponent_points = {
        team_id: fsum(
            weekly_means[(opponent_by_team_week[(team_id, week)], week)]
            for week in weeks
        )
        / len(weeks)
        for team_id in team_names
    }
    schedule_ranks = _ordinal_ranks(
        team_names,
        lambda team_id: (-average_opponent_points[team_id],),
    )
    position_totals, positions = _position_totals(bundle)
    position_percentiles = {
        (team_id, position): _percentile(
            position_totals[(team_id, position)],
            tuple(position_totals[(other_id, position)] for other_id in team_names),
        )
        for team_id in team_names
        for position in positions
    }

    rows = []
    for team_id in sorted(team_names, key=lambda value: projected_ranks[value]):
        projection = projection_by_team[team_id]
        current = projection.current_standing
        games_played = current.wins + current.losses + current.ties
        rows.append(
            {
                "team_id": team_id,
                "team_name": team_names[team_id],
                "power_rank": power_ranks[team_id],
                "power_score": power_scores[team_id],
                "current_rank": projection.current_rank,
                "projected_rank": projected_ranks[team_id],
                "mean_projected_rank": projection.mean_rank,
                "standings_change": projection.current_rank
                - projected_ranks[team_id],
                "current_record": _record(
                    current.wins, current.losses, current.ties
                ),
                "projected_record": _record(
                    projection.expected_final_wins,
                    projection.expected_final_losses,
                    projection.expected_final_ties,
                ),
                "playoff_probability": projection.playoff_probability,
                "championship_probability": championship[team_id],
                "current_points_for": current.points_for,
                "current_points_against": current.points_against,
                "current_points_for_per_game": _per_game(
                    current.points_for, games_played
                ),
                "current_points_against_per_game": _per_game(
                    current.points_against, games_played
                ),
                "expected_final_points_for": projection.expected_final_points_for,
                "expected_final_points_against": (
                    projection.expected_final_points_against
                ),
                "average_projected_points": fsum(
                    weekly_means[(team_id, week)] for week in weeks
                )
                / len(weeks),
                "schedule_difficulty_rank": schedule_ranks[team_id],
                "average_opponent_points": average_opponent_points[team_id],
                "expected_remaining_win_rate": fsum(
                    win_points[(team_id, week)] for week in weeks
                )
                / (len(weeks) * baseline_projection.scenario_count),
                "rank_distribution": list(projection.rank_distribution),
                "seed_distribution": list(projection.seed_distribution),
                "weekly_outlook": [
                    _weekly_record(
                        team_id,
                        week,
                        team_names,
                        opponent_by_team_week,
                        weekly,
                        win_points,
                        baseline_projection.scenario_count,
                    )
                    for week in weeks
                ],
                "position_outlook": [
                    {
                        "position": position,
                        "projected_points": position_totals[(team_id, position)],
                        "league_percentile": position_percentiles[
                            (team_id, position)
                        ],
                    }
                    for position in positions
                ],
            }
        )

    result = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "snapshot_id": state.snapshot_id,
        "season": state.season,
        "first_remaining_week": state.first_remaining_week,
        "scenario_count": baseline_projection.scenario_count,
        "scenario_sampling": _scenario_sampling(
            bundle.scenario_config.scenario_count,
            baseline_projection.scenario_count,
        ),
        "playoff_team_count": state.playoff_rules.qualifier_count,
        "power_engine_mode": bundle.methodology_mode,
        "power_engine_notice": _power_notice(bundle.methodology_mode),
        "championship_model": {
            "kind": "field_conditioned_power_share_v1",
            "status": "modeled_estimate",
            "label": "Modeled title chance",
            "methodology": (
                "Within each simulated regular-season playoff field, title share "
                "is weighted by calibrated roster power; 10 power points doubles "
                "a team's weight. Shares are then averaged across scenarios."
            ),
            "limitations": (
                "Postseason weeks, matchups, byes, reseeding, and player outcomes "
                "are not simulated because the bundle contains regular-season "
                "projections only. This is not an exact championship probability."
            ),
            "power_points_per_doubling": _TITLE_POWER_POINTS_PER_DOUBLING,
        },
        "weekly_model": {
            "kind": "mean_optimized_correlated_scenarios_v1",
            "score_decimal_places": baseline_projection.score_decimal_places,
            "methodology": (
                "Each week's legal lineup is optimized from ensemble means, then "
                "evaluated across the bundle's correlated score scenarios."
            ),
        },
        "schedule_difficulty_model": {
            "rank_one": "hardest",
            "basis": "mean projected opponent score over remaining matchups",
        },
        "position_model": {
            "basis": "all-roster remaining-season ensemble points by listed position",
            "percentile_scale": "0_to_1_higher_is_better",
        },
        "weeks": list(weeks),
        "positions": list(positions),
        "teams": rows,
    }
    validate_dashboard_result(result)
    return result


def _scenario_summaries(bundle, baseline, scenarios, power_scores):
    state = bundle.state
    weeks = state.remaining_regular_season_weeks
    team_ids = tuple(team.team_id for team in state.teams)
    weekly = {(team_id, week): _Moments() for team_id in team_ids for week in weeks}
    win_points = {(team_id, week): 0.0 for team_id in team_ids for week in weeks}
    title_totals = {team_id: 0.0 for team_id in team_ids}
    standings = {row.team_id: row for row in state.standings}
    initial_records = new_records(standings)
    uses_history = validate_tiebreaker_inputs(state)
    quantum = Decimal(1).scaleb(-baseline.score_decimal_places)
    score_rounder = prepared_score_rounder(quantum)

    for scenario in scenarios:
        score_map = {
            (score.team_id, score.week): score_rounder(score.score)
            for score in scenario.scores
        }
        records = clone_records(initial_records)
        simulated = settle_remaining_matchups(
            state, records, score_map, uses_history
        )
        played_games = (
            (*state.completed_matchups, *simulated) if uses_history else ()
        )
        order = rank_teams(
            records,
            state,
            baseline.random_seed,
            scenario.scenario_id,
            played_games,
        )
        field = select_playoff_seeds(
            state,
            order,
            records,
            baseline.random_seed,
            scenario.scenario_id,
            played_games,
        )
        for team_id, share in _field_conditioned_title_shares(
            field, power_scores
        ).items():
            title_totals[team_id] += share

        adjusted_scores = dict(score_map)
        for matchup in state.remaining_matchups:
            left_key = (matchup.team1_id, matchup.week)
            adjusted_scores[left_key] = _add_score_adjustment(
                adjusted_scores[left_key], matchup.team1_score_adjustment
            )
            left = adjusted_scores[left_key]
            right = adjusted_scores[(matchup.team2_id, matchup.week)]
            if left == right:
                left_points = 0.5
            else:
                left_points = 1.0 if left > right else 0.0
            win_points[left_key] += left_points
            win_points[(matchup.team2_id, matchup.week)] += 1.0 - left_points
        for key, score in adjusted_scores.items():
            weekly[key].add(float(score))

    return weekly, win_points, title_totals


def _field_conditioned_title_shares(field, power_scores):
    """Allocate one title across an already-selected simulated playoff field."""

    field_ids = tuple(field)
    if not field_ids or len(set(field_ids)) != len(field_ids):
        raise ValueError("playoff field must contain unique team IDs")
    if any(team_id not in power_scores for team_id in field_ids):
        raise ValueError("playoff field contains a team without a power score")
    maximum = max(power_scores[team_id] for team_id in field_ids)
    weights = {
        team_id: 2.0
        ** ((power_scores[team_id] - maximum) / _TITLE_POWER_POINTS_PER_DOUBLING)
        for team_id in field_ids
    }
    denominator = fsum(weights.values())
    return {team_id: weights[team_id] / denominator for team_id in field_ids}


def _position_totals(bundle):
    team_by_player = {
        player_id: roster.team_id
        for roster in bundle.rosters
        for player_id in roster.player_ids
    }
    positions = _sorted_positions(
        {
            row.position
            for row in bundle.projections
            if row.canonical_player_id in team_by_player
        }
    )
    totals = {
        (team.team_id, position): 0.0
        for team in bundle.state.teams
        for position in positions
    }
    for row in bundle.projections:
        team_id = team_by_player.get(row.canonical_player_id)
        if team_id is not None and row.projected_fantasy_points is not None:
            totals[(team_id, row.position)] += row.projected_fantasy_points
    return totals, positions


def _weekly_record(team_id, week, names, opponents, weekly, wins, count):
    opponent_id = opponents[(team_id, week)]
    moments = weekly[(team_id, week)]
    return {
        "week": week,
        "projected_points": moments.mean,
        "uncertainty": moments.standard_deviation,
        "opponent_id": opponent_id,
        "opponent_name": names[opponent_id],
        "matchup_win_probability": wins[(team_id, week)] / count,
    }


def _opponents(matchups):
    result = {}
    for matchup in matchups:
        result[(matchup.team1_id, matchup.week)] = matchup.team2_id
        result[(matchup.team2_id, matchup.week)] = matchup.team1_id
    return result


def _record(wins, losses, ties):
    return {"wins": wins, "losses": losses, "ties": ties}


def _per_game(points, games):
    return None if games == 0 else points / games


def _ordinal_ranks(names, key):
    ordered = sorted(
        names,
        key=lambda team_id: (*key(team_id), names[team_id].casefold(), team_id),
    )
    return {team_id: rank for rank, team_id in enumerate(ordered, 1)}


def _percentile(value, values):
    if len(values) == 1:
        return 1.0
    less = sum(candidate < value for candidate in values)
    equal = sum(candidate == value for candidate in values)
    average_zero_based_rank = less + (equal - 1) / 2
    return average_zero_based_rank / (len(values) - 1)


def _sorted_positions(positions):
    order = {position: index for index, position in enumerate(_POSITION_ORDER)}
    return tuple(sorted(positions, key=lambda value: (order.get(value, len(order)), value)))


def _scenario_sampling(bundle_count, dashboard_count):
    capped = dashboard_count < bundle_count
    return {
        "bundle_scenario_count": bundle_count,
        "dashboard_scenario_count": dashboard_count,
        "capped": capped,
        "policy": "deterministic_prefix" if capped else "full_bundle_stream",
        "methodology": (
            f"Dashboard calculations use the first {dashboard_count:,} deterministic "
            f"draws from the bundle's {bundle_count:,}-scenario stream to keep the "
            "automatic local view responsive."
            if capped
            else "Dashboard calculations use the bundle's complete scenario stream."
        ),
    }


def _power_notice(mode):
    if mode == "exact":
        return (
            "Power scores use this bundle's calibrated FantasyPros-method model; "
            "exact-method claims remain limited to its attested trade scope."
        )
    return (
        "Power scores use this bundle's disclosed surrogate model and are "
        "approximations, not exact FantasyPros values."
    )


__all__ = ("build_league_dashboard",)
