from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trade_snapshot.ensemble import EnsembleProjection, ProviderObservation
from trade_snapshot.league_search import ResumableLeagueTradeSearch
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
from trade_snapshot.search_runner import TradeSearchSettings
from trade_snapshot.strength import (
    CalibrationMetadata,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
    StrengthModel,
)
from trade_snapshot.trade_impact import prepare_season_baseline
from trade_snapshot.trade_space import TeamRoster, TradeConstraints


POINTS = {"p": 12.0, "a": 10.0, "b": 8.0, "c": 6.0}


def build_search(*, counterparties=None):
    team_ids = tuple(POINTS)
    state = LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="profile-1",
        first_remaining_week=1,
        teams=tuple(LeagueTeam(team_id, team_id.upper()) for team_id in team_ids),
        standings=tuple(TeamStanding(team_id, 0, 0, 0, 0, 0) for team_id in team_ids),
        remaining_matchups=(
            FantasyMatchup(1, "p", "a"),
            FantasyMatchup(1, "b", "c"),
        ),
        roster_rules=RosterRules(1, ("FLEX",)),
        playoff_rules=PlayoffRules(
            qualifier_count=2,
            regular_season_end_week=1,
            playoff_weeks=(2,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )
    rosters = tuple(TeamRoster(team_id, (team_id,), 1, 1) for team_id in team_ids)
    projections = tuple(
        EnsembleProjection(
            canonical_player_id=player_id,
            snapshot_id="snapshot-1",
            scoring_profile_id="profile-1",
            season=2026,
            week=1,
            position="FLEX",
            status=ProjectionStatus.OBSERVED,
            provider_observations=(
                ProviderObservation(
                    "espn",
                    f"espn-{player_id}",
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
            nfl_game_id="G1",
            opponent_team_id=f"OPP-{player_id}",
            is_home=True,
        )
        for player_id, points in POINTS.items()
    )
    model = StrengthModel(
        role_definitions=(
            RoleDefinition("FLEX", RoleKind.STARTER, "FLEX", frozenset({"FLEX"})),
        ),
        players=tuple(
            PlayerStrength(player_id, points, frozenset({"FLEX"}), {"FLEX": 0})
            for player_id, points in POINTS.items()
        ),
        normalization_denominator=40,
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="profile-1",
        calibration=CalibrationMetadata(
            "https://cdn.fantasypros.com/assets/trade-analyzer.js",
            "1" * 64,
            "2" * 64,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
    )
    baseline = prepare_season_baseline(
        state,
        rosters,
        projections,
        tuple(PlayerEligibility(player_id, ("FLEX",)) for player_id in team_ids),
        CorrelatedScenarioConfig(4, 9, FactorLoadings(0, 0, 0, 1)),
    )
    return ResumableLeagueTradeSearch(
        rosters,
        "p",
        model,
        baseline,
        TradeConstraints(require_no_drops=True),
        TradeSearchSettings(-100, 1),
        counterparty_team_ids=counterparties,
    )


class LeagueTradeSearchTests(unittest.TestCase):
    def test_searches_every_other_team_and_resumes_completed_pairs(self):
        search = build_search()
        updates = []
        with TemporaryDirectory() as directory:
            first = search.run(directory, on_progress=updates.append)
            second = search.run(directory)
            database_count = len(tuple(Path(directory).glob("*.sqlite3")))
            self.assertEqual(first.progress.pair_count, 3)
            self.assertEqual(first.progress.completed_pair_count, 3)
            self.assertEqual(first.progress.examined_candidate_count, 3)
            self.assertEqual(first.progress.total_candidate_count, 3)
            self.assertEqual(first.progress.completion_fraction, 1)
            self.assertEqual(
                tuple(row.counterparty_team_id for row in first.pairs),
                ("a", "b", "c"),
            )
            self.assertEqual(len(first.qualified_trades), 3)
            self.assertEqual(first, second)
            self.assertEqual(updates[-1], first.progress)
            self.assertEqual(database_count, 3)

    def test_cancel_then_resume_continues_across_pair_boundaries(self):
        search = build_search()
        calls = 0

        def cancel_on_second_pair():
            nonlocal calls
            calls += 1
            return calls > 1

        with TemporaryDirectory() as directory:
            partial = search.run(directory, should_cancel=cancel_on_second_pair)
            complete = search.run(directory)

        self.assertTrue(partial.progress.cancelled)
        self.assertEqual(partial.progress.completed_pair_count, 1)
        self.assertEqual(partial.progress.examined_candidate_count, 1)
        self.assertEqual(len(partial.pairs), 2)
        self.assertFalse(complete.progress.cancelled)
        self.assertEqual(complete.progress.completed_pair_count, 3)
        self.assertEqual(complete.progress.examined_candidate_count, 3)

    def test_can_select_counterparties_and_reject_unknown_teams(self):
        selected = build_search(counterparties=("c", "a"))
        self.assertEqual(tuple(team_id for team_id, _ in selected.runners), ("c", "a"))
        with self.assertRaisesRegex(ValueError, "unknown counterparty"):
            build_search(counterparties=("missing",))


if __name__ == "__main__":
    unittest.main()
