from collections import Counter
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
from trade_snapshot.scenario_score_cache import ScenarioScoreCache
from trade_snapshot.score_scenarios import PreparedScoreScenarios
from trade_snapshot.trade_impact import (
    PreparedSeasonBaseline,
    prepare_season_baseline,
)
from trade_snapshot.trade_space import TeamRoster


SCENARIO_COUNT = 17
TEAM_IDS = ("a", "b", "c", "d")
WEEKS = (11, 12)


class PreparedSeasonBaselineReuseTests(unittest.TestCase):
    def test_candidate_cache_layout_is_validated_once_per_projection(self):
        baseline = make_baseline()
        after_rosters = reroute_players(
            baseline.scenarios.rosters,
            {"a-rb": "b", "b-rb": "a"},
        )
        original = ScenarioScoreCache._validated_candidate_lineups
        calls = []

        def recording_validation(cache, prepared):
            calls.append(prepared)
            return original(cache, prepared)

        with patch.object(
            ScenarioScoreCache,
            "_validated_candidate_lineups",
            recording_validation,
        ):
            baseline.project(after_rosters)

        self.assertEqual(len(calls), 1)

    def test_realized_baseline_stream_replays_without_rescoring(self):
        baseline = make_baseline()
        expected = tuple(baseline.scenarios)
        calls = []
        original = PreparedScoreScenarios._team_week_score

        def recording_score(prepared, team_id, week, scenario_index, draw_cache):
            calls.append((team_id, week, scenario_index))
            return original(prepared, team_id, week, scenario_index, draw_cache)

        with patch.object(
            PreparedScoreScenarios,
            "_team_week_score",
            recording_score,
        ):
            actual = tuple(baseline.iter_baseline_scenarios())

        self.assertEqual(actual, expected)
        self.assertEqual(calls, [])

    def test_two_changed_teams_match_uncached_projection_with_selective_scoring(self):
        baseline = make_baseline()
        after_rosters = reroute_players(
            baseline.scenarios.rosters,
            {"a-rb": "b", "b-rb": "a"},
        )

        optimized, optimized_calls = project_with_score_calls(
            baseline, after_rosters
        )
        uncached, uncached_calls = project_with_score_calls(
            without_score_cache(baseline), after_rosters
        )

        self.assertEqual(optimized, uncached)
        self.assertNotEqual(
            optimized.after_scenario_run_id,
            optimized.before_scenario_run_id,
        )
        self.assertScoreCalls(optimized_calls, ("a", "b"))
        self.assertScoreCalls(uncached_calls, TEAM_IDS)

    def test_three_changed_teams_match_uncached_projection_with_selective_scoring(self):
        baseline = make_baseline()
        after_rosters = reroute_players(
            baseline.scenarios.rosters,
            {"a-rb": "b", "b-rb": "c", "c-rb": "a"},
        )

        optimized, optimized_calls = project_with_score_calls(
            baseline, after_rosters
        )
        uncached, uncached_calls = project_with_score_calls(
            without_score_cache(baseline), after_rosters
        )

        self.assertEqual(optimized, uncached)
        self.assertNotEqual(
            optimized.after_scenario_run_id,
            optimized.before_scenario_run_id,
        )
        self.assertScoreCalls(optimized_calls, ("a", "b", "c"))
        self.assertScoreCalls(uncached_calls, TEAM_IDS)

    def test_bench_only_change_reuses_exact_baseline_projection_without_scoring(self):
        baseline = make_baseline()
        after_rosters = reroute_players(
            baseline.scenarios.rosters,
            {"a-bench": "b", "b-bench": "a"},
        )

        optimized, optimized_calls = project_with_score_calls(
            baseline, after_rosters
        )
        uncached, _ = project_with_score_calls(
            without_score_cache(baseline), after_rosters
        )

        self.assertEqual(optimized, uncached)
        self.assertEqual(optimized_calls, ())
        self.assertIs(optimized.before, baseline.season_projection)
        self.assertIsNot(optimized.after, baseline.season_projection)
        self.assertEqual(
            optimized.after.teams,
            baseline.season_projection.teams,
        )
        self.assertEqual(
            optimized.after.scenario_run_id,
            optimized.after_scenario_run_id,
        )
        self.assertNotEqual(
            optimized.after_scenario_run_id,
            optimized.before_scenario_run_id,
        )

    def assertScoreCalls(self, calls, changed_team_ids):
        expected = Counter(
            {
                (team_id, week): SCENARIO_COUNT
                for team_id in changed_team_ids
                for week in WEEKS
            }
        )
        actual = Counter((team_id, week) for team_id, week, _ in calls)

        self.assertEqual(actual, expected)
        self.assertEqual(
            {scenario_index for _, _, scenario_index in calls},
            set(range(SCENARIO_COUNT)),
        )


def make_baseline() -> PreparedSeasonBaseline:
    state = LeagueState(
        snapshot_id="snapshot-impact-reuse",
        season=2026,
        scoring_profile_id="half-ppr-impact-reuse",
        first_remaining_week=11,
        teams=(
            LeagueTeam("a", "Alpha"),
            LeagueTeam("b", "Bravo"),
            LeagueTeam("c", "Charlie"),
            LeagueTeam("d", "Delta"),
        ),
        standings=(
            TeamStanding("a", 7, 3, 0, 1210, 1080),
            TeamStanding("b", 6, 4, 0, 1160, 1100),
            TeamStanding("c", 4, 6, 0, 1090, 1150),
            TeamStanding("d", 3, 7, 0, 1040, 1170),
        ),
        remaining_matchups=(
            FantasyMatchup(11, "a", "b"),
            FantasyMatchup(11, "c", "d"),
            FantasyMatchup(12, "a", "c"),
            FantasyMatchup(12, "b", "d"),
        ),
        roster_rules=RosterRules(5, ("QB", "RB", "WR", "FLEX")),
        playoff_rules=PlayoffRules(
            qualifier_count=2,
            regular_season_end_week=12,
            playoff_weeks=(13, 14),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(
                Tiebreaker.WIN_PERCENTAGE,
                Tiebreaker.POINTS_FOR,
                Tiebreaker.RANDOM_DRAW,
            ),
        ),
    )
    rosters = tuple(
        TeamRoster(
            team_id,
            tuple(f"{team_id}-{role}" for role in ("qb", "rb", "wr", "flex", "bench")),
            5,
            5,
        )
        for team_id in TEAM_IDS
    )
    role_eligibility = {
        "qb": ("QB",),
        "rb": ("RB", "FLEX"),
        "wr": ("WR", "FLEX"),
        "flex": ("FLEX",),
        "bench": ("FLEX",),
    }
    weekly_points = {
        "qb": (24.0, 23.0),
        "rb": (18.0, 17.0),
        "wr": (16.0, 15.0),
        "flex": (12.0, 13.0),
        "bench": (3.0, 4.0),
    }
    projections = []
    eligibilities = []
    for team_index, team_id in enumerate(TEAM_IDS):
        for role, eligible_slots in role_eligibility.items():
            player_id = f"{team_id}-{role}"
            eligibilities.append(PlayerEligibility(player_id, eligible_slots))
            for week, base_points in zip(WEEKS, weekly_points[role], strict=True):
                projections.append(
                    projection(
                        player_id,
                        role.upper(),
                        week,
                        base_points - team_index * 0.35,
                        nfl_team_id=f"NFL-{team_id.upper()}-{role.upper()}",
                    )
                )
    config = CorrelatedScenarioConfig(
        SCENARIO_COUNT,
        8675309,
        FactorLoadings(0.2, 0.3, 0.4, math.sqrt(0.71)),
    )
    return prepare_season_baseline(
        state,
        rosters,
        projections,
        eligibilities,
        config,
        score_decimal_places=2,
        tiebreak_random_seed=314159,
    )


def projection(
    player_id: str,
    position: str,
    week: int,
    points: float,
    *,
    nfl_team_id: str,
) -> EnsembleProjection:
    observation = ProviderObservation(
        provider="espn",
        provider_player_id=f"source-{player_id}-{week}",
        status=ProjectionStatus.OBSERVED,
        projected_fantasy_points=points,
        weight=1,
    )
    return EnsembleProjection(
        canonical_player_id=player_id,
        snapshot_id="snapshot-impact-reuse",
        scoring_profile_id="half-ppr-impact-reuse",
        season=2026,
        week=week,
        position=position,
        status=ProjectionStatus.OBSERVED,
        provider_observations=(observation,),
        minimum_observed_sources=1,
        position_stddev_floor=4.0,
        projected_fantasy_points=points,
        between_provider_stddev=0.0,
        predictive_stddev=4.0,
        nfl_team_id=nfl_team_id,
        nfl_game_id=f"GAME-{week}-{nfl_team_id}",
        opponent_team_id=f"OPP-{week}-{nfl_team_id}",
        is_home=week % 2 == 1,
    )


def reroute_players(
    rosters: tuple[TeamRoster, ...], destinations: dict[str, str]
) -> tuple[TeamRoster, ...]:
    players_by_team = {
        roster.team_id: [
            player_id
            for player_id in roster.player_ids
            if player_id not in destinations
        ]
        for roster in rosters
    }
    for player_id, destination_team_id in destinations.items():
        players_by_team[destination_team_id].append(player_id)
    return tuple(
        TeamRoster(
            roster.team_id,
            tuple(players_by_team[roster.team_id]),
            roster.current_size,
            roster.roster_cap,
        )
        for roster in rosters
    )


def without_score_cache(baseline: PreparedSeasonBaseline) -> PreparedSeasonBaseline:
    return PreparedSeasonBaseline(
        baseline.state,
        baseline.scenarios,
        baseline.season_projection,
        baseline.score_decimal_places,
        baseline.tiebreak_random_seed,
    )


def project_with_score_calls(baseline, rosters):
    original = PreparedScoreScenarios._team_week_score
    calls = []

    def recording_score(prepared, team_id, week, scenario_index, draw_cache):
        calls.append((team_id, week, scenario_index))
        return original(prepared, team_id, week, scenario_index, draw_cache)

    with patch.object(
        PreparedScoreScenarios,
        "_team_week_score",
        recording_score,
    ):
        result = baseline.project(rosters)
    return result, tuple(calls)


if __name__ == "__main__":
    unittest.main()
