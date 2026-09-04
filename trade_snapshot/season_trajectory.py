"""Observed-plus-simulated record trajectories for trade-timing decisions."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from math import ceil, fsum
from statistics import median

from ._scenario_random import content_id
from ._season_ranking import (
    _add_score_adjustment,
    new_records,
    rank_teams,
    round_score,
    select_playoff_seeds,
    settle_remaining_matchups,
    validate_tiebreaker_inputs,
)
from .league_state import CompletedFantasyMatchup, LeagueState
from ._record_trend import (
    RECORD_SLOPE_NEUTRAL_BAND,
    record_slope_direction,
    trailing_record_slope,
)
from .season import ScoreScenario


_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class RecordTriggerScenarios:
    """Scenario indexes where a loss and downward record slope coincide."""

    scenario_indexes: tuple[int, ...]
    eligible_scenario_count: int
    total_scenario_count: int

    @property
    def probability(self) -> float | None:
        return (
            None
            if self.eligible_scenario_count == 0
            else len(self.scenario_indexes) / self.eligible_scenario_count
        )


def build_season_trajectory(
    state: LeagueState,
    scenarios: Iterable[ScoreScenario],
    *,
    score_decimal_places: int = 2,
    random_seed: int = 0,
) -> dict[str, object]:
    """Append correlated future outcomes to a standings-consistent actual record path."""

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if type(score_decimal_places) is not int or not 0 <= score_decimal_places <= 9:
        raise ValueError("score_decimal_places must be an integer from 0 through 9")
    if type(random_seed) is not int:
        raise ValueError("random_seed must be an integer")
    scenario_rows = tuple(scenarios)
    if not scenario_rows:
        raise ValueError("at least one score scenario is required")

    names = {row.team_id: row.name for row in state.teams}
    team_ids = tuple(sorted(names, key=lambda team_id: (names[team_id].casefold(), team_id)))
    weeks = state.remaining_regular_season_weeks
    observed = _observed_trajectories(state, names)
    aggregates = {
        (team_id, week): _new_aggregate()
        for team_id in team_ids
        for week in weeks
    }
    quantum = Decimal(1).scaleb(-score_decimal_places)
    standings = {row.team_id: row for row in state.standings}
    expected_score_keys = {
        (team_id, week) for team_id in team_ids for week in weeks
    }
    uses_history = validate_tiebreaker_inputs(state)
    seen_scenario_ids: set[str] = set()

    for scenario in scenario_rows:
        _validate_scenario(state, scenario, expected_score_keys, seen_scenario_ids)
        score_map = {
            (row.team_id, row.week): round_score(row.score, quantum)
            for row in scenario.scores
        }
        records = new_records(standings)
        simulated = settle_remaining_matchups(state, records, score_map, uses_history)
        played_games = (*state.completed_matchups, *simulated) if uses_history else ()
        order = rank_teams(
            records,
            state,
            random_seed,
            scenario.scenario_id,
            played_games,
        )
        playoff_teams = frozenset(
            select_playoff_seeds(
                state,
                order,
                records,
                random_seed,
                scenario.scenario_id,
                played_games,
            )
        )
        _accumulate_scenario(
            state,
            score_map,
            observed,
            standings,
            playoff_teams,
            aggregates,
        )

    projected = {
        team_id: [
            _projected_record(
                team_id,
                week,
                names,
                state,
                aggregates[(team_id, week)],
                len(scenario_rows),
            )
            for week in weeks
        ]
        for team_id in team_ids
    }
    _add_pressure_percentiles(projected, weeks)
    standing_by_team = {row.team_id: row for row in state.standings}
    teams = [
        {
            "team_id": team_id,
            "team_name": names[team_id],
            "current_record": _record_dict(standing_by_team[team_id]),
            "current_direction": _current_direction(
                None if observed is None else observed[team_id]
            ),
            "observed": [] if observed is None else observed[team_id],
            "projected": projected[team_id],
        }
        for team_id in team_ids
    ]
    scenario_ids = sorted(row.scenario_id for row in scenario_rows)
    scenario_identity = {
        "host_snapshot_id": state.snapshot_id,
        "scoring_profile_id": state.scoring_profile_id,
        "scenario_ids": scenario_ids,
    }
    return {
        "schema_version": _SCHEMA_VERSION,
        "snapshot_id": state.snapshot_id,
        "scenario_count": len(scenario_rows),
        "scenario_evidence": {
            "scenario_set_id": content_id(
                "trajectory-scenario-set", scenario_identity
            ),
            "host_snapshot_id": state.snapshot_id,
            "scoring_profile_id": state.scoring_profile_id,
            "first_scenario_id": scenario_ids[0],
            "last_scenario_id": scenario_ids[-1],
            "scenario_count": len(scenario_rows),
        },
        "history_status": (
            "complete" if observed is not None else "unavailable_or_inconsistent"
        ),
        "history_coverage": {
            "completed_matchups_usable": observed is not None,
            "observed_trajectory_status": (
                "complete" if observed is not None else "withheld"
            ),
            "limitation": (
                None
                if observed is not None
                else "Completed matchup history is missing or inconsistent with standings."
            ),
        },
        "methodology": {
            "record_value": "win=1, tie=0.5, loss=0",
            "slope": (
                "Theil-Sen median slope over the latest four cumulative "
                "win-equivalent percentages"
            ),
            "slope_neutral_band_per_week": RECORD_SLOPE_NEUTRAL_BAND,
            "pressure_percentile": (
                "Equal-weight league percentile of loss probability, downward-slope "
                "probability, and playoff sensitivity when each component is available."
            ),
            "pressure_is_acceptance_probability": False,
            "conditional_playoff_note": (
                "Playoff values conditional on a simulated win or loss are associations "
                "inside the shared scenario ensemble, not causal effects."
            ),
        },
        "teams": teams,
    }


def build_loss_and_downward_scenario_index(
    state: LeagueState,
    scenarios: Iterable[ScoreScenario],
    *,
    score_decimal_places: int = 2,
) -> dict[tuple[str, int], RecordTriggerScenarios]:
    """Index the exact paths that satisfy each team's loss/downturn trigger."""

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if type(score_decimal_places) is not int or not 0 <= score_decimal_places <= 9:
        raise ValueError("score_decimal_places must be an integer from 0 through 9")
    scenario_rows = tuple(scenarios)
    if not scenario_rows:
        raise ValueError("at least one score scenario is required")
    team_ids = tuple(sorted(team.team_id for team in state.teams))
    weeks = state.remaining_regular_season_weeks
    keys = tuple((team_id, week) for team_id in team_ids for week in weeks)
    matched = {key: [] for key in keys}
    eligible = {key: 0 for key in keys}
    observed = _observed_trajectories(
        state, {team.team_id: team.name for team in state.teams}
    )
    standings = {row.team_id: row for row in state.standings}
    expected_score_keys = {(team_id, week) for team_id in team_ids for week in weeks}
    quantum = Decimal(1).scaleb(-score_decimal_places)
    seen_scenario_ids: set[str] = set()

    for scenario_index, scenario in enumerate(scenario_rows):
        _validate_scenario(
            state, scenario, expected_score_keys, seen_scenario_ids
        )
        score_map = {
            (row.team_id, row.week): round_score(row.score, quantum)
            for row in scenario.scores
        }
        outcomes = _future_outcomes(state, score_map)
        for team_id in team_ids:
            for path in _team_future_path(
                state,
                team_id,
                standings[team_id],
                None if observed is None else observed[team_id],
                outcomes,
            ):
                slope = path["record_slope"]
                if slope is None:
                    continue
                key = team_id, path["week"]
                eligible[key] += 1
                if (
                    path["outcome"] == "loss"
                    and slope < -RECORD_SLOPE_NEUTRAL_BAND
                ):
                    matched[key].append(scenario_index)

    total = len(scenario_rows)
    return {
        key: RecordTriggerScenarios(tuple(matched[key]), eligible[key], total)
        for key in keys
    }


def _observed_trajectories(state, names):
    if not state.completed_history_is_usable:
        return None
    by_team = {team_id: [] for team_id in names}
    records = {
        team_id: {"wins": 0, "losses": 0, "ties": 0}
        for team_id in names
    }
    points = {team_id: [] for team_id in names}
    for matchup in sorted(state.completed_matchups, key=lambda row: row.week):
        _append_observed(matchup, matchup.team1_id, matchup.team2_id, records, points, by_team, names)
        _append_observed(matchup, matchup.team2_id, matchup.team1_id, records, points, by_team, names)
    return by_team


def _append_observed(matchup, team_id, opponent_id, records, points, rows, names):
    team_score, opponent_score = _completed_scores(matchup, team_id)
    outcome = "win" if team_score > opponent_score else "loss" if team_score < opponent_score else "tie"
    record_field = {"win": "wins", "loss": "losses", "tie": "ties"}[outcome]
    records[team_id][record_field] += 1
    record = records[team_id]
    played = record["wins"] + record["losses"] + record["ties"]
    win_percentage = (record["wins"] + 0.5 * record["ties"]) / played
    points[team_id].append((matchup.week, win_percentage))
    slope = trailing_record_slope(points[team_id])
    rows[team_id].append(
        {
            "week": matchup.week,
            "kind": "observed",
            "opponent_id": opponent_id,
            "opponent_name": names[opponent_id],
            "outcome": outcome,
            "team_score": team_score,
            "opponent_score": opponent_score,
            "cumulative_wins": record["wins"],
            "cumulative_losses": record["losses"],
            "cumulative_ties": record["ties"],
            "cumulative_win_percentage": win_percentage,
            "record_slope": slope,
            "direction": record_slope_direction(slope),
        }
    )


def _completed_scores(matchup: CompletedFantasyMatchup, team_id: str):
    if matchup.team1_id == team_id:
        return matchup.team1_score, matchup.team2_score
    return matchup.team2_score, matchup.team1_score


def _new_aggregate():
    return {
        "wins": 0,
        "ties": 0,
        "losses": 0,
        "cumulative_wins": 0.0,
        "cumulative_losses": 0.0,
        "cumulative_ties": 0.0,
        "win_percentages": [],
        "slopes": [],
        "downward": 0,
        "upward": 0,
        "slope_count": 0,
        "two_loss_streak": 0,
        "streak_count": 0,
        "loss_and_downward": 0,
        "trigger_count": 0,
        "playoff_after_win": 0,
        "playoff_after_loss": 0,
        "win_count": 0,
        "loss_count": 0,
    }


def _accumulate_scenario(state, score_map, observed, standings, playoff_teams, aggregates):
    outcomes = _future_outcomes(state, score_map)
    for team in state.teams:
        team_id = team.team_id
        for path in _team_future_path(
            state,
            team_id,
            standings[team_id],
            None if observed is None else observed[team_id],
            outcomes,
        ):
            week = path["week"]
            outcome = path["outcome"]
            slope = path["record_slope"]
            aggregate = aggregates[(team_id, week)]
            aggregate[{"win": "wins", "loss": "losses", "tie": "ties"}[outcome]] += 1
            aggregate["cumulative_wins"] += path["cumulative_wins"]
            aggregate["cumulative_losses"] += path["cumulative_losses"]
            aggregate["cumulative_ties"] += path["cumulative_ties"]
            aggregate["win_percentages"].append(path["cumulative_win_percentage"])
            if slope is not None:
                aggregate["slopes"].append(slope)
                aggregate["slope_count"] += 1
                downward = slope < -RECORD_SLOPE_NEUTRAL_BAND
                upward = slope > RECORD_SLOPE_NEUTRAL_BAND
                aggregate["downward"] += int(downward)
                aggregate["upward"] += int(upward)
                aggregate["trigger_count"] += 1
                aggregate["loss_and_downward"] += int(outcome == "loss" and downward)
            if path["previous_outcome"] is not None:
                aggregate["streak_count"] += 1
                aggregate["two_loss_streak"] += int(
                    path["previous_outcome"] == "loss" and outcome == "loss"
                )
            made_playoffs = team_id in playoff_teams
            if outcome == "win":
                aggregate["win_count"] += 1
                aggregate["playoff_after_win"] += int(made_playoffs)
            elif outcome == "loss":
                aggregate["loss_count"] += 1
                aggregate["playoff_after_loss"] += int(made_playoffs)


def _team_future_path(state, team_id, standing, observed_rows, outcomes):
    wins, losses, ties = standing.wins, standing.losses, standing.ties
    history_points = (
        [(row["week"], row["cumulative_win_percentage"]) for row in observed_rows]
        if observed_rows is not None
        else []
    )
    if not history_points and state.first_remaining_week > 1:
        played = wins + losses + ties
        if played:
            history_points.append(
                (state.first_remaining_week - 1, (wins + 0.5 * ties) / played)
            )
    previous_outcome = observed_rows[-1]["outcome"] if observed_rows else None
    for week in state.remaining_regular_season_weeks:
        outcome = outcomes[(team_id, week)]
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        else:
            ties += 1
        played = wins + losses + ties
        win_percentage = (wins + 0.5 * ties) / played
        history_points.append((week, win_percentage))
        yield {
            "week": week,
            "outcome": outcome,
            "previous_outcome": previous_outcome,
            "cumulative_wins": wins,
            "cumulative_losses": losses,
            "cumulative_ties": ties,
            "cumulative_win_percentage": win_percentage,
            "record_slope": trailing_record_slope(history_points),
        }
        previous_outcome = outcome


def _future_outcomes(state, score_map):
    outcomes = {}
    for matchup in state.remaining_matchups:
        left = _add_score_adjustment(
            score_map[(matchup.team1_id, matchup.week)],
            matchup.team1_score_adjustment,
        )
        right = score_map[(matchup.team2_id, matchup.week)]
        left_outcome = "win" if left > right else "loss" if left < right else "tie"
        right_outcome = "loss" if left_outcome == "win" else "win" if left_outcome == "loss" else "tie"
        outcomes[(matchup.team1_id, matchup.week)] = left_outcome
        outcomes[(matchup.team2_id, matchup.week)] = right_outcome
    return outcomes


def _projected_record(team_id, week, names, state, aggregate, count):
    opponent_id = _opponent(state, team_id, week)
    minimum_group = max(100, ceil(count * 0.05))
    win_conditional = (
        aggregate["playoff_after_win"] / aggregate["win_count"]
        if aggregate["win_count"] >= minimum_group
        else None
    )
    loss_conditional = (
        aggregate["playoff_after_loss"] / aggregate["loss_count"]
        if aggregate["loss_count"] >= minimum_group
        else None
    )
    return {
        "week": week,
        "kind": "projected",
        "opponent_id": opponent_id,
        "opponent_name": names[opponent_id],
        "win_probability": aggregate["wins"] / count,
        "tie_probability": aggregate["ties"] / count,
        "loss_probability": aggregate["losses"] / count,
        "expected_cumulative_wins": aggregate["cumulative_wins"] / count,
        "expected_cumulative_losses": aggregate["cumulative_losses"] / count,
        "expected_cumulative_ties": aggregate["cumulative_ties"] / count,
        "median_cumulative_win_percentage": median(aggregate["win_percentages"]),
        "cumulative_win_percentage_p10": _quantile(aggregate["win_percentages"], 0.10),
        "cumulative_win_percentage_p90": _quantile(aggregate["win_percentages"], 0.90),
        "median_record_slope": median(aggregate["slopes"]) if aggregate["slopes"] else None,
        "record_slope_p10": _quantile(aggregate["slopes"], 0.10),
        "record_slope_p90": _quantile(aggregate["slopes"], 0.90),
        "downward_slope_probability": _rate(aggregate["downward"], aggregate["slope_count"]),
        "upward_slope_probability": _rate(aggregate["upward"], aggregate["slope_count"]),
        "two_loss_streak_probability": _rate(
            aggregate["two_loss_streak"], aggregate["streak_count"]
        ),
        "loss_and_downward_trigger_probability": _rate(
            aggregate["loss_and_downward"], aggregate["trigger_count"]
        ),
        "playoff_probability_if_win": win_conditional,
        "playoff_probability_if_loss": loss_conditional,
        "playoff_sensitivity": (
            None
            if win_conditional is None or loss_conditional is None
            else max(0.0, win_conditional - loss_conditional)
        ),
        "conditional_win_scenario_count": aggregate["win_count"],
        "conditional_loss_scenario_count": aggregate["loss_count"],
        "conditional_minimum_scenario_count": minimum_group,
        "pressure_percentile": None,
        "pressure_components": [],
    }


def _add_pressure_percentiles(projected, weeks):
    for week in weeks:
        rows = [next(row for row in values if row["week"] == week) for values in projected.values()]
        component_names = (
            "loss_probability",
            "downward_slope_probability",
            "playoff_sensitivity",
        )
        for component in component_names:
            available = [row[component] for row in rows]
            if any(value is None for value in available):
                continue
            for row in rows:
                row.setdefault("_pressure_values", []).append(
                    _league_percentile(row[component], available)
                )
                row["pressure_components"].append(component)
        for row in rows:
            values = row.pop("_pressure_values", [])
            row["pressure_percentile"] = fsum(values) / len(values) if values else None


def _current_direction(rows):
    return (
        "unavailable"
        if rows is None
        else record_slope_direction(rows[-1]["record_slope"] if rows else None)
    )


def _quantile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _league_percentile(value, values):
    if len(values) == 1:
        return 0.5
    less = sum(candidate < value for candidate in values)
    equal = sum(candidate == value for candidate in values)
    return (less + (equal - 1) / 2) / (len(values) - 1)


def _rate(numerator, denominator):
    return None if denominator == 0 else numerator / denominator


def _opponent(state, team_id, week):
    for matchup in state.remaining_matchups:
        if matchup.week == week and team_id in (matchup.team1_id, matchup.team2_id):
            return matchup.team2_id if matchup.team1_id == team_id else matchup.team1_id
    raise AssertionError("validated schedule is missing an opponent")


def _record_dict(standing):
    return {
        "wins": standing.wins,
        "losses": standing.losses,
        "ties": standing.ties,
    }


def _validate_scenario(state, scenario, expected_keys, seen_ids):
    if not isinstance(scenario, ScoreScenario):
        raise ValueError("scenarios must contain ScoreScenario values")
    if scenario.snapshot_id != state.snapshot_id or scenario.scoring_profile_id != state.scoring_profile_id:
        raise ValueError("scenario does not match the selected league state")
    if scenario.scenario_id in seen_ids:
        raise ValueError("scenario_id values must be unique")
    seen_ids.add(scenario.scenario_id)
    actual_keys = {(row.team_id, row.week) for row in scenario.scores}
    if actual_keys != expected_keys:
        raise ValueError("scenario must contain exactly one score for every remaining team-week")


__all__ = (
    "RecordTriggerScenarios",
    "build_loss_and_downward_scenario_index",
    "build_season_trajectory",
)
