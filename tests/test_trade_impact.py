from dataclasses import replace
import unittest
from unittest.mock import patch

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
from trade_snapshot.scenario_config import (
    CorrelatedScenarioConfig,
    FactorLoadings,
    PlayerEligibility,
)
from trade_snapshot.trade_impact import (
    PreparedSeasonBaseline,
    prepare_season_baseline,
    project_roster_change,
)
from trade_snapshot.trade_space import TeamRoster


def state():
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
        roster_rules=RosterRules(1, ("FLEX",)),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=1,
            playoff_weeks=(2,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def projection(player_id, points):
    return EnsembleProjection(
        canonical_player_id=player_id,
        snapshot_id="snapshot-1",
        scoring_profile_id="profile-1",
        season=2026,
        week=1,
        position="FLEX",
        status=ProjectionStatus.OBSERVED,
        provider_observations=(
            ProviderObservation(
                "espn", f"espn-{player_id}", ProjectionStatus.OBSERVED, points, 1
            ),
        ),
        minimum_observed_sources=1,
        position_stddev_floor=0,
        projected_fantasy_points=points,
        between_provider_stddev=0,
        predictive_stddev=0,
        nfl_team_id=f"NFL-{player_id}",
        nfl_game_id="G1",
        opponent_team_id=f"OPP-{player_id}",
        is_home=True,
    )


def inputs():
    before = (
        TeamRoster("a", ("p1",), 1, 1),
        TeamRoster("b", ("p2",), 1, 1),
    )
    projections = (projection("p1", 10.0), projection("p2", 5.0))
    eligibility = (
        PlayerEligibility("p1", ("FLEX",)),
        PlayerEligibility("p2", ("FLEX",)),
    )
    config = CorrelatedScenarioConfig(3, 19, FactorLoadings(0, 0, 0, 1))
    return before, projections, eligibility, config


class PairedSeasonProjectionTests(unittest.TestCase):
    def test_trade_uses_common_draws_and_reports_both_team_changes(self):
        before, projections, eligibility, config = inputs()
        after = (
            TeamRoster("a", ("p2",), 1, 1),
            TeamRoster("b", ("p1",), 1, 1),
        )
        result = project_roster_change(
            state(), before, after, projections, eligibility, config
        )

        self.assertEqual(result.before.scenario_count, 3)
        self.assertEqual(result.for_team("a").playoff_probability_delta, -1.0)
        self.assertEqual(result.for_team("b").playoff_probability_delta, 1.0)
        self.assertEqual(result.for_team("a").expected_wins_delta, -1.0)
        self.assertEqual(result.for_team("a").mean_rank_delta, 1.0)
        self.assertNotEqual(result.before_scenario_run_id, result.after_scenario_run_id)
        self.assertTrue(result.impact_id.startswith("impact_"))

    def test_no_op_is_bit_for_bit_identical(self):
        before, projections, eligibility, config = inputs()
        result = project_roster_change(
            state(), reversed(before), before, reversed(projections), reversed(eligibility), config
        )

        self.assertEqual(result.before, result.after)
        self.assertEqual(result.before_scenario_run_id, result.after_scenario_run_id)
        self.assertTrue(
            all(change.playoff_probability_delta == 0 for change in result.changes)
        )

    def test_one_prepared_baseline_serves_multiple_candidates(self):
        before, projections, eligibility, config = inputs()
        baseline = prepare_season_baseline(
            state(), before, projections, eligibility, config
        )
        swapped = (
            TeamRoster("a", ("p2",), 1, 1),
            TeamRoster("b", ("p1",), 1, 1),
        )

        no_op = baseline.project(before)
        trade = baseline.project(swapped)

        self.assertIs(no_op.before, baseline.season_projection)
        self.assertIs(trade.before, baseline.season_projection)
        self.assertEqual(no_op.for_team("a").playoff_probability_delta, 0)
        self.assertEqual(trade.for_team("a").playoff_probability_delta, -1)

    def test_oversized_score_cache_uses_the_existing_streaming_projection(self):
        before, projections, eligibility, config = inputs()
        with patch(
            "trade_snapshot.trade_impact.ScenarioScoreCacheBuilder.for_prepared",
            return_value=None,
        ):
            baseline = prepare_season_baseline(
                state(), before, projections, eligibility, config
            )

        self.assertIsNone(baseline._score_cache)
        self.assertEqual(baseline.project(before).before, baseline.season_projection)

    def test_add_drop_uses_the_same_full_projection_draw_space(self):
        before, projections, eligibility, config = inputs()
        after = (
            TeamRoster("a", ("p1",), 1, 1),
            TeamRoster("b", ("p3",), 1, 1),
        )
        projections = (*projections, projection("p3", 15.0))
        eligibility = (*eligibility, PlayerEligibility("p3", ("FLEX",)))
        result = project_roster_change(
            state(), before, after, projections, eligibility, config
        )
        self.assertTrue(result.draw_space_id.startswith("sdraw_"))
        self.assertGreater(result.for_team("b").playoff_probability_delta, 0)

        unknown = (
            TeamRoster("a", ("p1",), 1, 1),
            TeamRoster("b", ("unknown",), 1, 1),
        )
        with self.assertRaisesRegex(ValueError, "every rostered player"):
            project_roster_change(
                state(), before, unknown, projections, eligibility, config
            )

    def test_unknown_team_lookup_is_explicit(self):
        before, projections, eligibility, config = inputs()
        result = project_roster_change(
            state(), before, before, projections, eligibility, config
        )
        with self.assertRaises(KeyError):
            result.for_team("missing")

    def test_baseline_rejects_mixed_season_projection_identity(self):
        before, projections, eligibility, config = inputs()
        baseline = prepare_season_baseline(
            state(), before, projections, eligibility, config
        )
        changed = replace(
            baseline.season_projection,
            scoring_profile_id="other-profile",
        )

        with self.assertRaisesRegex(ValueError, "league state"):
            PreparedSeasonBaseline(
                baseline.state,
                baseline.scenarios,
                changed,
                baseline.score_decimal_places,
                baseline.tiebreak_random_seed,
            )


if __name__ == "__main__":
    unittest.main()
