from dataclasses import replace
import inspect
import json
import math
import unittest

from trade_snapshot.ensemble import EnsembleProjection, ProviderObservation
from trade_snapshot.league_state import (
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)
from trade_snapshot.projections import ProjectionStatus
from trade_snapshot.score_scenarios import (
    CorrelatedScenarioConfig,
    FactorLoadings,
    PlayerEligibility,
    prepare_score_scenarios,
)
from trade_snapshot.trade_space import TeamRoster
from trade_snapshot.season import project_remaining_season


class ScenarioConfigTests(unittest.TestCase):
    def test_config_has_strict_lossless_json_record_and_content_id(self):
        config = CorrelatedScenarioConfig(
            3, -9, FactorLoadings(0, 0, 0, 1), player_score_floor=-5
        )
        record = config.to_record()

        json.dumps(record, allow_nan=False)
        self.assertEqual(CorrelatedScenarioConfig.from_record(record), config)
        self.assertTrue(config.config_id.startswith("scfg_"))
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["player_score_floor"], -5.0)

        invalid = (
            {**record, "extra": True},
            {**record, "scenario_count": True},
            {**record, "config_id": "scfg_tampered"},
            {**record, "algorithm": "random-v2"},
            {**record, "loadings": {**record["loadings"], "extra": 0}},
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CorrelatedScenarioConfig.from_record(value)

    def test_invalid_factor_loadings_and_options_fail_closed(self):
        bad_loadings = (
            (-1, 0, 0, 1),
            (math.inf, 0, 0, 1),
            (math.nan, 0, 0, 1),
            (True, 0, 0, 1),
            (0.5, 0, 0, 0.5),
        )
        for values in bad_loadings:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    FactorLoadings(*values)

        for count in (0, -1, True, 1 << 53):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    CorrelatedScenarioConfig(count, 0, FactorLoadings(0, 0, 0, 1))
        with self.assertRaises(ValueError):
            CorrelatedScenarioConfig(1, 1 << 53, FactorLoadings(0, 0, 0, 1))
        for floor in (True, math.inf, math.nan, "0"):
            with self.subTest(floor=floor), self.assertRaises(ValueError):
                CorrelatedScenarioConfig(
                    1, 0, FactorLoadings(0, 0, 0, 1), floor
                )


class PreparedScoreScenarioTests(unittest.TestCase):
    def test_is_deterministic_and_independent_of_input_order(self):
        state = make_state(("FLEX",), roster_cap=2)
        rosters = (
            TeamRoster("a", ("p1", "p2"), 2, 2),
            TeamRoster("b", ("p3", "p4"), 2, 2),
        )
        projections = tuple(
            projection(player, mean, nfl_team=f"NFL-{player}", game=f"G-{player}")
            for player, mean in zip(("p1", "p2", "p3", "p4"), (15, 10, 14, 9))
        )
        eligibility = tuple(PlayerEligibility(player, ("FLEX",)) for player in ("p1", "p2", "p3", "p4"))

        first = prepare_score_scenarios(state, rosters, projections, eligibility, config_for(4, seed=17))
        reversed_rosters = tuple(
            TeamRoster(row.team_id, tuple(reversed(row.player_ids)), row.current_size, row.roster_cap)
            for row in reversed(rosters)
        )
        second = prepare_score_scenarios(
            state,
            reversed_rosters,
            reversed(projections),
            reversed(eligibility),
            config_for(4, seed=17),
        )

        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.identity_record(), second.identity_record())
        self.assertEqual(tuple(first), tuple(second))
        json.dumps(first.identity_record(), allow_nan=False)

    def test_capacity_exempt_membership_is_preserved_in_scenario_identity(self):
        ordinary = basic_prepared(scenario_count=2)
        with_ir = prepare_score_scenarios(
            ordinary.state,
            (
                TeamRoster("a", ("p1",), 1, 1, {"p1"}),
                ordinary.rosters[1],
            ),
            ordinary.projections,
            ordinary.eligibilities,
            ordinary.config,
        )

        self.assertEqual(with_ir.draw_space_id, ordinary.draw_space_id)
        self.assertNotEqual(with_ir.run_id, ordinary.run_id)
        self.assertEqual(
            with_ir.rosters[0].capacity_exempt_player_ids,
            frozenset({"p1"}),
        )
        self.assertEqual(
            with_ir.identity_record()["rosters"][0][
                "capacity_exempt_player_ids"
            ],
            ["p1"],
        )
        scores = {
            row.team_id: row.score
            for row in next(iter(with_ir)).scores
        }
        self.assertEqual(scores["a"], 0.0)

    def test_matchup_score_adjustment_changes_content_identity_not_player_draws(self):
        ordinary = basic_prepared(scenario_count=2)
        adjusted_state = replace(
            ordinary.state,
            remaining_matchups=(
                replace(
                    ordinary.state.remaining_matchups[0],
                    team1_score_adjustment=1,
                ),
            ),
        )
        adjusted = prepare_score_scenarios(
            adjusted_state,
            ordinary.rosters,
            ordinary.projections,
            ordinary.eligibilities,
            ordinary.config,
        )

        self.assertEqual(adjusted.draw_space_id, ordinary.draw_space_id)
        self.assertNotEqual(adjusted.run_id, ordinary.run_id)
        self.assertEqual(adjusted.identity_record()["schema_version"], 3)
        self.assertEqual(
            adjusted.identity_record()["remaining_matchups"][0][
                "team1_score_adjustment"
            ],
            1,
        )

    def test_mean_optimal_lineup_finds_global_rb_flex_assignment(self):
        state = make_state(("RB", "FLEX"), roster_cap=3)
        rosters = (
            TeamRoster("a", ("rb-only", "dual", "wr"), 3, 3),
            TeamRoster("b", ("b-rb", "b-flex"), 2, 3),
        )
        means = {"rb-only": 9, "dual": 12, "wr": 11, "b-rb": 8, "b-flex": 7}
        projections = tuple(
            projection(player, mean, stddev=0, nfl_team=f"N-{player}", game=f"G-{player}")
            for player, mean in means.items()
        )
        eligibility = (
            PlayerEligibility("rb-only", ("RB",)),
            PlayerEligibility("dual", ("RB", "FLEX")),
            PlayerEligibility("wr", ("FLEX",)),
            PlayerEligibility("b-rb", ("RB",)),
            PlayerEligibility("b-flex", ("FLEX",)),
        )

        result = next(iter(prepare_score_scenarios(state, rosters, projections, eligibility, config_for(1))))
        scores = {score.team_id: score.score for score in result.scores}

        self.assertEqual(scores["a"], 23.0)
        self.assertEqual(scores["b"], 15.0)

    def test_bye_is_benched_and_missing_player_week_is_not_silently_zeroed(self):
        state = make_state(("FLEX",), roster_cap=2)
        rosters = (
            TeamRoster("a", ("bye", "active"), 2, 2),
            TeamRoster("b", ("other",), 1, 2),
        )
        projections = (
            projection("bye", None, bye=True, nfl_team="NFL-A", game=None),
            projection("active", 7, stddev=0, nfl_team="NFL-A", game="G1"),
            projection("other", 5, stddev=0, nfl_team="NFL-B", game="G1"),
        )
        eligibility = tuple(PlayerEligibility(player, ("FLEX",)) for player in ("bye", "active", "other"))

        prepared = prepare_score_scenarios(state, rosters, projections, eligibility, config_for(1))
        scores = {score.team_id: score.score for score in next(iter(prepared)).scores}

        self.assertEqual(prepared.player_score("bye", 1, 0), 0.0)
        self.assertEqual(scores["a"], 7.0)
        with self.assertRaisesRegex(ValueError, "every rostered player/week"):
            prepare_score_scenarios(state, rosters, projections[1:], eligibility, config_for(1))

    def test_trade_pair_uses_the_same_player_draws_and_no_op_is_exact(self):
        state = make_state(("FLEX",), roster_cap=2)
        before = (
            TeamRoster("a", ("p1", "p2"), 2, 2),
            TeamRoster("b", ("p3", "p4"), 2, 2),
        )
        after = (
            TeamRoster("a", ("p3", "p2"), 2, 2),
            TeamRoster("b", ("p1", "p4"), 2, 2),
        )
        contexts = {
            "p1": ("NFL-A", "G1"), "p2": ("NFL-A", "G1"),
            "p3": ("NFL-B", "G1"), "p4": ("NFL-C", "G2"),
        }
        projections = tuple(
            projection(player, 10 + index, stddev=4, nfl_team=team, game=game)
            for index, (player, (team, game)) in enumerate(contexts.items())
        )
        eligibility = tuple(PlayerEligibility(player, ("FLEX",)) for player in contexts)
        loadings = FactorLoadings(0.2, 0.3, 0.4, math.sqrt(0.71))
        config = CorrelatedScenarioConfig(8, 23, loadings)

        left = prepare_score_scenarios(state, before, projections, eligibility, config)
        right = prepare_score_scenarios(state, after, projections, eligibility, config)
        no_op = prepare_score_scenarios(state, reversed(before), reversed(projections), reversed(eligibility), config)

        self.assertEqual(left.draw_space_id, right.draw_space_id)
        self.assertNotEqual(left.run_id, right.run_id)
        self.assertEqual(tuple(left), tuple(no_op))
        for index in range(config.scenario_count):
            self.assertEqual(left.player_score("p1", 1, index), right.player_score("p1", 1, index))

    def test_stream_supports_bounded_resume_without_materializing_results(self):
        prepared = basic_prepared(scenario_count=5)

        stream = prepared.iter_scenarios(2, 4)
        self.assertTrue(inspect.isgenerator(stream))
        scenarios = tuple(stream)

        self.assertEqual(len(scenarios), 2)
        self.assertTrue(scenarios[0].scenario_id.endswith(":2"))
        self.assertTrue(scenarios[1].scenario_id.endswith(":3"))
        self.assertEqual(len(scenarios[0].scores), 2)
        season = project_remaining_season(prepared.state, iter(prepared))
        self.assertEqual(season.scenario_count, 5)
        self.assertEqual(sum(team.playoff_probability for team in season.teams), 1.0)
        with self.assertRaises(ValueError):
            tuple(prepared.iter_scenarios(0, 6))

    def test_context_required_only_for_enabled_factors_and_score_floor_is_explicit(self):
        state = make_state(("FLEX",), roster_cap=1)
        rosters = (TeamRoster("a", ("p1",), 1, 1), TeamRoster("b", ("p2",), 1, 1))
        projections = (
            projection("p1", 0, stddev=50, nfl_team=None, game=None),
            projection("p2", 0, stddev=50, nfl_team=None, game=None),
        )
        eligibility = (PlayerEligibility("p1", ("FLEX",)), PlayerEligibility("p2", ("FLEX",)))

        player_only = prepare_score_scenarios(
            state, rosters, projections, eligibility, config_for(100)
        )
        self.assertTrue(
            any(score.score < 0 for scenario in player_only for score in scenario.scores)
        )
        floored = prepare_score_scenarios(
            state,
            rosters,
            projections,
            eligibility,
            CorrelatedScenarioConfig(
                100, 0, FactorLoadings(0, 0, 0, 1), player_score_floor=0
            ),
        )
        self.assertTrue(
            all(score.score >= 0 for scenario in floored for score in scenario.scores)
        )

        with self.assertRaisesRegex(ValueError, "game IDs"):
            prepare_score_scenarios(
                state, rosters, projections, eligibility,
                CorrelatedScenarioConfig(1, 0, FactorLoadings(0, 1, 0, 0)),
            )

    def test_empirical_factor_correlations_match_variance_shares(self):
        state = make_state(("FLEX",), roster_cap=2)
        rosters = (
            TeamRoster("a", ("same-team-1", "same-game"), 2, 2),
            TeamRoster("b", ("same-team-2", "other-game"), 2, 2),
        )
        contexts = {
            "same-team-1": ("NFL-A", "G1"),
            "same-team-2": ("NFL-A", "G1"),
            "same-game": ("NFL-B", "G1"),
            "other-game": ("NFL-C", "G2"),
        }
        projections = tuple(
            projection(player, 100, stddev=5, nfl_team=team, game=game)
            for player, (team, game) in contexts.items()
        )
        eligibility = tuple(PlayerEligibility(player, ("FLEX",)) for player in contexts)
        loadings = FactorLoadings(0.2, 0.4, 0.5, math.sqrt(0.55))
        prepared = prepare_score_scenarios(
            state, rosters, projections, eligibility,
            CorrelatedScenarioConfig(5000, 991, loadings),
        )
        values = {
            player: [prepared.player_score(player, 1, index) for index in range(5000)]
            for player in contexts
        }

        self.assertAlmostEqual(correlation(values["same-team-1"], values["same-team-2"]), 0.45, delta=0.05)
        self.assertAlmostEqual(correlation(values["same-team-1"], values["same-game"]), 0.20, delta=0.05)
        self.assertAlmostEqual(correlation(values["same-team-1"], values["other-game"]), 0.04, delta=0.05)


def config_for(count, seed=0):
    return CorrelatedScenarioConfig(count, seed, FactorLoadings(0, 0, 0, 1))


def make_state(slots, roster_cap):
    return LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="profile-1",
        first_remaining_week=1,
        teams=(LeagueTeam("a", "Alpha"), LeagueTeam("b", "Bravo")),
        standings=(
            TeamStanding("a", 0, 0, 0, 0, 0),
            TeamStanding("b", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(FantasyMatchup(1, "a", "b"),),
        roster_rules=RosterRules(roster_cap, slots),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=1,
            playoff_weeks=(2,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def projection(player, points, *, stddev=3, nfl_team="NFL-A", game="G1", bye=False):
    status = ProjectionStatus.BYE if bye else ProjectionStatus.OBSERVED
    observation = ProviderObservation(
        provider="ensemble-source",
        provider_player_id=f"source-{player}",
        status=status,
        projected_fantasy_points=None if bye else points,
        weight=1,
    )
    return EnsembleProjection(
        canonical_player_id=player,
        snapshot_id="snapshot-1",
        scoring_profile_id="profile-1",
        season=2026,
        week=1,
        position="FLEX",
        status=status,
        provider_observations=(observation,),
        minimum_observed_sources=1,
        position_stddev_floor=stddev,
        projected_fantasy_points=None if bye else float(points),
        between_provider_stddev=None if bye else 0.0,
        predictive_stddev=None if bye else float(stddev),
        nfl_team_id=nfl_team,
        nfl_game_id=None if bye else game,
        opponent_team_id=None if bye or game is None else f"OPP-{nfl_team}",
        is_home=None if bye or game is None else True,
    )


def basic_prepared(scenario_count):
    state = make_state(("FLEX",), roster_cap=1)
    rosters = (TeamRoster("a", ("p1",), 1, 1), TeamRoster("b", ("p2",), 1, 1))
    projections = (projection("p1", 10), projection("p2", 11, nfl_team="NFL-B"))
    eligibility = (PlayerEligibility("p1", ("FLEX",)), PlayerEligibility("p2", ("FLEX",)))
    return prepare_score_scenarios(state, rosters, projections, eligibility, config_for(scenario_count))


def correlation(left, right):
    left_mean = math.fsum(left) / len(left)
    right_mean = math.fsum(right) / len(right)
    covariance = math.fsum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = math.fsum((x - left_mean) ** 2 for x in left)
    right_ss = math.fsum((y - right_mean) ** 2 for y in right)
    return covariance / math.sqrt(left_ss * right_ss)


if __name__ == "__main__":
    unittest.main()
