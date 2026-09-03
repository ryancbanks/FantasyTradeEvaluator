from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import trade_snapshot.season_trajectory as trajectory_module
from trade_snapshot.league_state import (
    CompletedFantasyMatchup,
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)
from trade_snapshot.season import ScoreScenario, TeamWeekScore
from trade_snapshot.season_trajectory import (
    build_loss_and_downward_scenario_index,
    build_season_trajectory,
    build_season_trajectory_analysis,
)


def _state():
    return LeagueState(
        snapshot_id="trajectory-snapshot",
        season=2026,
        scoring_profile_id="trajectory-profile",
        first_remaining_week=4,
        teams=(LeagueTeam("a", "Alpha"), LeagueTeam("b", "Bravo")),
        standings=(
            TeamStanding("a", 2, 1, 0, 300, 270),
            TeamStanding("b", 1, 2, 0, 270, 300),
        ),
        completed_matchups=(
            CompletedFantasyMatchup(1, "a", "b", 110, 90),
            CompletedFantasyMatchup(2, "a", "b", 100, 80),
            CompletedFantasyMatchup(3, "a", "b", 90, 100),
        ),
        remaining_matchups=(
            FantasyMatchup(4, "a", "b"),
            FantasyMatchup(5, "a", "b"),
        ),
        roster_rules=RosterRules(1, ("FLEX",)),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=5,
            playoff_weeks=(6,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def _scenarios():
    scores = (
        ((10, 5), (10, 5)),
        ((4, 5), (10, 5)),
        ((4, 5), (4, 5)),
        ((5, 5), (5, 5)),
    )
    return tuple(
        ScoreScenario(
            f"scenario-{index}",
            "trajectory-snapshot",
            "trajectory-profile",
            tuple(
                TeamWeekScore(team_id, week, value)
                for week, pair in zip((4, 5), weeks, strict=True)
                for team_id, value in zip(("a", "b"), pair, strict=True)
            ),
        )
        for index, weeks in enumerate(scores)
    )


class SeasonTrajectoryTests(unittest.TestCase):
    def test_appends_simulated_paths_to_exact_observed_record(self):
        result = build_season_trajectory(_state(), _scenarios())
        alpha = next(row for row in result["teams"] if row["team_id"] == "a")
        week4 = alpha["projected"][0]

        self.assertEqual(result["history_status"], "complete")
        self.assertEqual(alpha["current_direction"], "downward")
        self.assertEqual(
            [row["outcome"] for row in alpha["observed"]],
            ["win", "win", "loss"],
        )
        self.assertEqual(week4["win_probability"], 0.25)
        self.assertEqual(week4["loss_probability"], 0.5)
        self.assertEqual(week4["tie_probability"], 0.25)
        self.assertAlmostEqual(
            week4["win_probability"]
            + week4["loss_probability"]
            + week4["tie_probability"],
            1,
        )
        self.assertIsNone(week4["playoff_probability_if_win"])
        self.assertEqual(week4["conditional_minimum_scenario_count"], 100)
        self.assertIsNotNone(week4["pressure_percentile"])
        self.assertFalse(result["methodology"]["pressure_is_acceptance_probability"])
        json.dumps(result, allow_nan=False, sort_keys=True)

    def test_missing_completed_ledger_never_becomes_invented_history(self):
        state = replace(_state(), completed_matchups=())

        result = build_season_trajectory(state, _scenarios())

        self.assertEqual(result["history_status"], "unavailable_or_inconsistent")
        self.assertTrue(all(not row["observed"] for row in result["teams"]))
        self.assertTrue(all(row["current_direction"] == "unavailable" for row in result["teams"]))

    def test_loss_downturn_index_matches_the_exact_pre_trade_scenario_paths(self):
        result = build_loss_and_downward_scenario_index(_state(), _scenarios())

        alpha_week4 = result[("a", 4)]
        self.assertEqual(alpha_week4.scenario_indexes, (1, 2))
        self.assertEqual(alpha_week4.eligible_scenario_count, 4)
        self.assertEqual(alpha_week4.total_scenario_count, 4)
        self.assertEqual(alpha_week4.probability, 0.5)

    def test_combined_analysis_builds_the_same_trigger_index_in_one_validation_pass(self):
        scenarios = _scenarios()
        with patch(
            "trade_snapshot.season_trajectory._validate_scenario",
            wraps=trajectory_module._validate_scenario,
        ) as validate:
            trajectory, trigger_index = build_season_trajectory_analysis(
                _state(), scenarios
            )

        self.assertEqual(validate.call_count, len(scenarios))
        self.assertEqual(
            trigger_index,
            build_loss_and_downward_scenario_index(_state(), scenarios),
        )
        self.assertEqual(trajectory, build_season_trajectory(_state(), scenarios))

    def test_duplicate_or_detached_scenarios_fail_closed(self):
        scenarios = _scenarios()
        with self.assertRaisesRegex(ValueError, "unique"):
            build_season_trajectory(_state(), (scenarios[0], scenarios[0]))
        detached = replace(scenarios[0], snapshot_id="another")
        with self.assertRaisesRegex(ValueError, "match"):
            build_season_trajectory(_state(), (detached,))


if __name__ == "__main__":
    unittest.main()
