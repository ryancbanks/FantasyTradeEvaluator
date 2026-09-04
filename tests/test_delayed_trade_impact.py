import unittest
from unittest.mock import patch

from trade_snapshot.delayed_trade_impact import (
    PreparedDelayedBaseline,
    PreparedDelayedRosterChange,
    prepare_delayed_baseline,
    prepare_delayed_roster_change,
)
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
from trade_snapshot.season import project_remaining_season
from trade_snapshot.trade_impact import prepare_season_baseline
from trade_snapshot.trade_space import TeamRoster


def _projection(player_id, week, points):
    return EnsembleProjection(
        canonical_player_id=player_id,
        snapshot_id="delayed-snapshot",
        scoring_profile_id="delayed-profile",
        season=2026,
        week=week,
        position="FLEX",
        status=ProjectionStatus.OBSERVED,
        provider_observations=(
            ProviderObservation(
                "source",
                f"source-{player_id}",
                ProjectionStatus.OBSERVED,
                points,
                1,
            ),
        ),
        minimum_observed_sources=1,
        position_stddev_floor=0,
        projected_fantasy_points=points,
        between_provider_stddev=0,
        predictive_stddev=0,
        nfl_team_id=f"NFL-{player_id}",
        nfl_game_id=f"G{week}",
        opponent_team_id=f"OPP-{player_id}",
        is_home=True,
    )


def _inputs():
    state = LeagueState(
        snapshot_id="delayed-snapshot",
        season=2026,
        scoring_profile_id="delayed-profile",
        first_remaining_week=1,
        teams=(LeagueTeam("a", "Alpha"), LeagueTeam("b", "Bravo")),
        standings=(
            TeamStanding("a", 0, 0, 0, 0, 0),
            TeamStanding("b", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(
            FantasyMatchup(1, "a", "b"),
            FantasyMatchup(2, "a", "b"),
        ),
        roster_rules=RosterRules(1, ("FLEX",)),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=2,
            playoff_weeks=(3,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )
    before = (
        TeamRoster("a", ("p1",), 1, 1),
        TeamRoster("b", ("p2",), 1, 1),
    )
    after = (
        TeamRoster("a", ("p2",), 1, 1),
        TeamRoster("b", ("p1",), 1, 1),
    )
    projections = tuple(
        _projection(player, week, points)
        for week in (1, 2)
        for player, points in (("p1", 10), ("p2", 5))
    )
    eligibility = (
        PlayerEligibility("p1", ("FLEX",)),
        PlayerEligibility("p2", ("FLEX",)),
    )
    config = CorrelatedScenarioConfig(4, 9, FactorLoadings(0, 0, 0, 1))
    return state, before, after, projections, eligibility, config


class DelayedTradeImpactTests(unittest.TestCase):
    def test_execute_now_matches_existing_immediate_trade_projection(self):
        state, before, after, projections, eligibility, config = _inputs()
        baseline = prepare_season_baseline(
            state, before, projections, eligibility, config
        )
        delayed = prepare_delayed_roster_change(
            baseline, after, ("a", "b")
        )

        self.assertEqual(delayed.project(1).after, baseline.project(after).after)
        self.assertEqual(
            delayed.project(1).for_team("a").expected_wins_delta,
            -2,
        )

    def test_future_execution_preserves_every_pre_effective_week(self):
        state, before, after, projections, eligibility, config = _inputs()
        baseline = prepare_season_baseline(
            state, before, projections, eligibility, config
        )
        delayed = prepare_delayed_roster_change(
            baseline,
            after,
            ("b", "a"),
        )

        result = delayed.project(2)

        self.assertEqual(result.for_team("a").expected_wins_delta, -1)
        self.assertEqual(result.for_team("b").expected_wins_delta, 1)
        self.assertEqual(tuple(delayed.project_many((2, 1))), (1, 2))
        with self.assertRaisesRegex(ValueError, "remaining"):
            delayed.project(3)

    def test_conditioned_projection_reuses_only_the_selected_pre_trade_paths(self):
        state, before, after, projections, eligibility, config = _inputs()
        baseline = prepare_season_baseline(
            state, before, projections, eligibility, config
        )
        delayed = prepare_delayed_baseline(baseline).roster_change(
            after, ("a", "b")
        )

        results = delayed.project_conditioned_many((1, 2), (1, 3))

        self.assertEqual(tuple(results), (1, 2))
        self.assertEqual(results[1].before.scenario_count, 2)
        self.assertEqual(
            results[1].before_scenario_run_id,
            results[2].before_scenario_run_id,
        )
        self.assertNotEqual(
            results[1].before_scenario_run_id,
            baseline.scenarios.run_id,
        )
        with self.assertRaisesRegex(ValueError, "unique integer"):
            delayed.project_conditioned(2, (1, 1))
        with self.assertRaisesRegex(ValueError, "outside"):
            delayed.project_conditioned(2, (4,))

    def test_delayed_baseline_materializes_its_own_trusted_scenarios(self):
        state, before, _, projections, eligibility, config = _inputs()
        baseline = prepare_season_baseline(
            state, before, projections, eligibility, config
        )

        prepared = PreparedDelayedBaseline(baseline)

        self.assertEqual(prepared.before_scenarios, tuple(baseline.scenarios))
        with self.assertRaises(TypeError):
            PreparedDelayedBaseline(baseline, prepared.before_scenarios)
        with self.assertRaisesRegex(ValueError, "PreparedDelayedBaseline"):
            PreparedDelayedRosterChange(baseline, before, ("a", "b"))

    def test_conditioned_before_projection_is_reused_across_candidate_trades(self):
        state, before, after, projections, eligibility, config = _inputs()
        baseline = prepare_season_baseline(
            state, before, projections, eligibility, config
        )
        prepared = prepare_delayed_baseline(baseline)
        first = prepared.roster_change(after, ("a", "b"))
        second = prepared.roster_change(after, ("a", "b"))

        with patch(
            "trade_snapshot.delayed_trade_impact.project_remaining_season",
            wraps=project_remaining_season,
        ) as project:
            first.project_conditioned(2, (1, 3))
            second.project_conditioned(2, (1, 3))

        # One shared conditioned baseline plus one changed projection per trade.
        self.assertEqual(project.call_count, 3)

    def test_changed_team_contract_fails_closed(self):
        state, before, after, projections, eligibility, config = _inputs()
        baseline = prepare_season_baseline(
            state, before, projections, eligibility, config
        )

        with self.assertRaisesRegex(ValueError, "cover every"):
            prepare_delayed_roster_change(baseline, after, ("a",))
        with self.assertRaisesRegex(ValueError, "outside"):
            prepare_delayed_roster_change(baseline, after, ("a", "b", "missing"))


if __name__ == "__main__":
    unittest.main()
