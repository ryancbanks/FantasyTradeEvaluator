"""Strict input and output invariants for league-dashboard analysis."""

import json
from math import fsum, isfinite
from numbers import Real

from .score_scenarios import PreparedScoreScenarios
from .season import SeasonProjection, TeamSeasonProjection


_PROBABILITY_TOLERANCE = 1e-9


def validate_dashboard_inputs(bundle, projection, scenarios) -> None:
    if not isinstance(projection, SeasonProjection):
        raise ValueError("baseline_projection must be a SeasonProjection")
    if not isinstance(scenarios, PreparedScoreScenarios):
        raise ValueError("scenarios must be PreparedScoreScenarios")
    state = bundle.state
    if (
        projection.snapshot_id != state.snapshot_id
        or projection.scoring_profile_id != state.scoring_profile_id
        or projection.scenario_count != scenarios.config.scenario_count
    ):
        raise ValueError("baseline projection does not match the engine bundle")
    if (
        scenarios.state != state
        or scenarios.rosters != bundle.rosters
        or scenarios.projections != bundle.projections
        or scenarios.eligibilities != bundle.eligibilities
        or scenarios.config.seed != bundle.scenario_config.seed
        or scenarios.config.loadings != bundle.scenario_config.loadings
        or scenarios.config.scenario_count > bundle.scenario_config.scenario_count
    ):
        raise ValueError("prepared scenarios do not match the engine bundle")

    team_ids = tuple(team.team_id for team in state.teams)
    rows = tuple(projection.teams)
    if any(not isinstance(row, TeamSeasonProjection) for row in rows):
        raise ValueError(
            "baseline projection teams must contain TeamSeasonProjection values"
        )
    if {row.team_id for row in rows} != set(team_ids) or len(rows) != len(team_ids):
        raise ValueError(
            "baseline projection must contain every league team exactly once"
        )
    standings = {row.team_id: row for row in state.standings}
    if any(row.current_standing != standings[row.team_id] for row in rows):
        raise ValueError(
            "baseline projection current standings do not match the bundle"
        )
    if any(type(row.current_rank) is not int for row in rows) or sorted(
        row.current_rank for row in rows
    ) != list(range(1, len(rows) + 1)):
        raise ValueError("baseline projection current ranks are invalid")

    for row in rows:
        _validate_team_projection(row, len(rows), state.playoff_rules.qualifier_count)
        final_games = (
            row.expected_final_wins
            + row.expected_final_losses
            + row.expected_final_ties
        )
        current_games = (
            row.current_standing.wins
            + row.current_standing.losses
            + row.current_standing.ties
        )
        if (
            abs(
                final_games
                - current_games
                - len(state.remaining_regular_season_weeks)
            )
            > _PROBABILITY_TOLERANCE
        ):
            raise ValueError("baseline projected record has the wrong game count")

    qualifier_count = state.playoff_rules.qualifier_count
    if (
        abs(fsum(row.playoff_probability for row in rows) - qualifier_count)
        > _PROBABILITY_TOLERANCE
    ):
        raise ValueError(
            "baseline playoff probabilities violate league qualifier count"
        )
    for rank in range(len(rows)):
        if (
            abs(fsum(row.rank_distribution[rank] for row in rows) - 1.0)
            > _PROBABILITY_TOLERANCE
        ):
            raise ValueError("baseline rank slot probabilities must sum to one")
    for seed in range(qualifier_count):
        if (
            abs(fsum(row.seed_distribution[seed] for row in rows) - 1.0)
            > _PROBABILITY_TOLERANCE
        ):
            raise ValueError("baseline seed slot probabilities must sum to one")


def _validate_team_projection(row, team_count, qualifier_count) -> None:
    numbers = (
        row.expected_final_wins,
        row.expected_final_losses,
        row.expected_final_ties,
        row.expected_final_points_for,
        row.expected_final_points_against,
        row.mean_rank,
        row.playoff_probability,
        *row.rank_distribution,
        *row.seed_distribution,
    )
    if any(not _is_finite_number(value) for value in numbers):
        raise ValueError("baseline projection contains a non-finite number")
    if (
        len(row.rank_distribution) != team_count
        or len(row.seed_distribution) != qualifier_count
    ):
        raise ValueError("baseline projection distribution dimensions are invalid")
    probabilities = (
        row.playoff_probability,
        *row.rank_distribution,
        *row.seed_distribution,
    )
    if any(value < 0 or value > 1 for value in probabilities):
        raise ValueError(
            "baseline projection probabilities must be between zero and one"
        )
    if (
        min(
            row.expected_final_wins,
            row.expected_final_losses,
            row.expected_final_ties,
        )
        < 0
        or not 1 <= row.mean_rank <= team_count
    ):
        raise ValueError("baseline projection record or mean rank is invalid")
    if abs(fsum(row.rank_distribution) - 1.0) > _PROBABILITY_TOLERANCE:
        raise ValueError("baseline rank distribution must sum to one")
    if (
        abs(fsum(row.seed_distribution) - row.playoff_probability)
        > _PROBABILITY_TOLERANCE
    ):
        raise ValueError(
            "baseline seed distribution must sum to playoff probability"
        )


def _is_finite_number(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, Real)
        and isfinite(value)
    )


def validate_dashboard_result(result) -> None:
    teams = result["teams"]
    comparison = result["fantasypros_comparison"]
    if comparison["team_count"] != len(teams):
        raise AssertionError("FantasyPros comparison must cover every dashboard team")
    rank_matches = sum(
        row["fantasypros_comparison"]["current_rank_match"] for row in teams
    )
    record_matches = sum(
        row["fantasypros_comparison"]["current_record_match"] for row in teams
    )
    if (
        comparison["current_rank_match_count"] != rank_matches
        or comparison["current_rank_all_match"] != (rank_matches == len(teams))
        or comparison["current_record_match_count"] != record_matches
        or comparison["current_record_all_match"] != (record_matches == len(teams))
    ):
        raise AssertionError("FantasyPros comparison summary does not reconcile")
    championship_sum = fsum(row["championship_probability"] for row in teams)
    if abs(championship_sum - 1.0) > _PROBABILITY_TOLERANCE:
        raise AssertionError("championship probabilities must sum to one")
    for row in teams:
        _validate_benchmark_comparison(row)
        title = row["championship_probability"]
        playoffs = row["playoff_probability"]
        if (
            title < -_PROBABILITY_TOLERANCE
            or title > playoffs + _PROBABILITY_TOLERANCE
        ):
            raise AssertionError(
                "championship probability must be bounded by playoff probability"
            )
        expected_points = row["current_points_for"] + fsum(
            week["projected_points"] for week in row["weekly_outlook"]
        )
        if abs(expected_points - row["expected_final_points_for"]) > 1e-7:
            raise AssertionError(
                "weekly outlook does not reconcile to projected points"
            )
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AssertionError("dashboard result must be strict JSON data") from error


def _validate_benchmark_comparison(row) -> None:
    comparison = row["fantasypros_comparison"]
    source = comparison["source"]
    deltas = comparison["local_minus_source"]
    expected = {
        "current_rank": row["current_rank"] - source["current_rank"],
        "projected_rank": row["mean_projected_rank"] - source["projected_rank"],
        "projected_wins": (
            row["projected_record"]["wins"]
            - source["projected_record"]["wins"]
        ),
        "projected_losses": (
            row["projected_record"]["losses"]
            - source["projected_record"]["losses"]
        ),
        "playoff_probability": (
            row["playoff_probability"] - source["playoff_probability"]
        ),
        "championship_probability": (
            row["championship_probability"]
            - source["championship_probability"]
        ),
    }
    if set(deltas) != set(expected) or any(
        not _is_finite_number(value)
        or abs(value - expected[name]) > _PROBABILITY_TOLERANCE
        for name, value in deltas.items()
    ):
        raise AssertionError("FantasyPros comparison deltas do not reconcile")
    if comparison["current_rank_match"] != (
        row["current_rank"] == source["current_rank"]
    ):
        raise AssertionError("FantasyPros current-rank comparison is inconsistent")


__all__ = ("validate_dashboard_inputs", "validate_dashboard_result")
