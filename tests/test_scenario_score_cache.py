import math
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
from trade_snapshot.scenario_score_cache import (
    DEFAULT_MAX_CACHE_BYTES,
    ScenarioScoreCacheBuilder,
)
from trade_snapshot.score_scenarios import (
    PreparedScoreScenarios,
    prepare_score_scenarios,
)
from trade_snapshot.season import project_remaining_season
from trade_snapshot.trade_space import TeamRoster


class ScenarioScoreCacheTests(unittest.TestCase):
    def test_captures_baseline_during_projection_and_replays_it_exactly(self):
        prepared = make_prepared(scenario_count=4)
        builder = ScenarioScoreCacheBuilder.for_prepared(prepared)

        self.assertIsNotNone(builder)
        assert builder is not None
        baseline_projection = project_remaining_season(prepared.state, builder)
        cache = builder.finish()

        expected_bytes = 4 * 2 * 2 * 8
        self.assertEqual(baseline_projection.scenario_count, 4)
        self.assertEqual(builder.max_bytes, DEFAULT_MAX_CACHE_BYTES)
        self.assertEqual(builder.estimated_byte_count, expected_bytes)
        self.assertEqual(cache.cached_byte_count, expected_bytes)
        self.assertEqual(cache.recomputed_cell_count(prepared), 0)
        self.assertEqual(tuple(cache.iter_scenarios(prepared)), tuple(prepared))
        with self.assertRaisesRegex(RuntimeError, "already finished"):
            builder.finish()

    def test_recomputes_only_changed_team_week_lineups(self):
        baseline = make_prepared(scenario_count=5)
        builder = ScenarioScoreCacheBuilder.for_prepared(baseline)
        assert builder is not None
        project_remaining_season(baseline.state, builder)
        cache = builder.finish()
        candidate = baseline.with_rosters(
            (
                TeamRoster("a", ("a-starter", "b-bench"), 2, 2),
                TeamRoster("b", ("b-starter", "a-bench"), 2, 2),
            )
        )
        expected = tuple(candidate)
        original = PreparedScoreScenarios._team_week_score
        calls = []

        def recording_score(prepared, team_id, week, scenario_index, draw_cache):
            calls.append((scenario_index, week, team_id))
            return original(prepared, team_id, week, scenario_index, draw_cache)

        self.assertEqual(cache.recomputed_cell_count(candidate), 10)
        with patch.object(
            PreparedScoreScenarios, "_team_week_score", recording_score
        ):
            actual = tuple(cache.iter_scenarios(candidate))

        self.assertEqual(actual, expected)
        self.assertEqual(len(calls), 10)
        self.assertEqual(
            tuple((row.team_id, row.week) for row in actual[0].scores),
            (("a", 1), ("b", 1), ("a", 2), ("b", 2)),
        )
        self.assertEqual(
            tuple(row.scenario_id for row in actual),
            tuple(row.scenario_id for row in expected),
        )

    def test_factory_declines_storage_over_bound(self):
        prepared = make_prepared(scenario_count=3)
        required_bytes = 3 * 2 * 2 * 8

        self.assertIsNone(
            ScenarioScoreCacheBuilder.for_prepared(
                prepared, max_bytes=required_bytes - 1
            )
        )
        exact = ScenarioScoreCacheBuilder.for_prepared(
            prepared, max_bytes=required_bytes
        )
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact.estimated_byte_count, required_bytes)
        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "max_bytes"):
                    ScenarioScoreCacheBuilder.for_prepared(
                        prepared, max_bytes=invalid
                    )

    def test_rejects_partial_capture_and_incompatible_candidates(self):
        prepared = make_prepared(scenario_count=3)
        partial = ScenarioScoreCacheBuilder.for_prepared(prepared)
        assert partial is not None
        stream = iter(partial)
        next(stream)
        stream.close()
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            partial.finish()

        builder = ScenarioScoreCacheBuilder.for_prepared(prepared)
        assert builder is not None
        tuple(builder)
        cache = builder.finish()
        incompatible = make_prepared(scenario_count=3, seed=99)
        with self.assertRaisesRegex(ValueError, "scenario config"):
            tuple(cache.iter_scenarios(incompatible))
        with self.assertRaisesRegex(ValueError, "stop cannot exceed"):
            tuple(cache.iter_scenarios(prepared, 0, 4))


def make_prepared(
    *, scenario_count: int, seed: int = 17
) -> PreparedScoreScenarios:
    state = LeagueState(
        snapshot_id="snapshot-cache",
        season=2026,
        scoring_profile_id="ppr-cache",
        first_remaining_week=1,
        teams=(LeagueTeam("b", "Bravo"), LeagueTeam("a", "Alpha")),
        standings=(
            TeamStanding("a", 0, 0, 0, 0, 0),
            TeamStanding("b", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(
            FantasyMatchup(1, "a", "b"),
            FantasyMatchup(2, "a", "b"),
        ),
        roster_rules=RosterRules(2, ("FLEX",)),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=2,
            playoff_weeks=(3,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(
                Tiebreaker.WIN_PERCENTAGE,
                Tiebreaker.RANDOM_DRAW,
            ),
        ),
    )
    rosters = (
        TeamRoster("a", ("a-starter", "a-bench"), 2, 2),
        TeamRoster("b", ("b-starter", "b-bench"), 2, 2),
    )
    points = {
        "a-starter": (20, 5),
        "a-bench": (10, 10),
        "b-starter": (9, 20),
        "b-bench": (8, 8),
    }
    projections = tuple(
        projection(player_id, week, weekly[week - 1])
        for player_id, weekly in points.items()
        for week in (1, 2)
    )
    eligibility = tuple(
        PlayerEligibility(player_id, ("FLEX",)) for player_id in points
    )
    config = CorrelatedScenarioConfig(
        scenario_count,
        seed,
        FactorLoadings(0.2, 0.3, 0.4, math.sqrt(0.71)),
    )
    return prepare_score_scenarios(
        state, rosters, projections, eligibility, config
    )


def projection(player_id: str, week: int, points: float) -> EnsembleProjection:
    observation = ProviderObservation(
        provider="cache-test",
        provider_player_id=f"source-{player_id}",
        status=ProjectionStatus.OBSERVED,
        projected_fantasy_points=points,
        weight=1,
    )
    return EnsembleProjection(
        canonical_player_id=player_id,
        snapshot_id="snapshot-cache",
        scoring_profile_id="ppr-cache",
        season=2026,
        week=week,
        position="FLEX",
        status=ProjectionStatus.OBSERVED,
        provider_observations=(observation,),
        minimum_observed_sources=1,
        position_stddev_floor=3,
        projected_fantasy_points=float(points),
        between_provider_stddev=0.0,
        predictive_stddev=3.0,
        nfl_team_id=f"NFL-{player_id}",
        nfl_game_id=f"G-{week}-{player_id}",
        opponent_team_id=f"OPP-{player_id}",
        is_home=True,
    )


if __name__ == "__main__":
    unittest.main()
