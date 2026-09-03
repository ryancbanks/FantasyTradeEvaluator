from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_search_runner import league_state, projection
from trade_snapshot.roster_adjustment import PreparedRosterAdjuster
from trade_snapshot.scenario_config import (
    CorrelatedScenarioConfig,
    FactorLoadings,
    PlayerEligibility,
)
from trade_snapshot.search import PreparedTradePair
from trade_snapshot.search_runner import ResumableTradeSearch, TradeSearchSettings
from trade_snapshot.strength import StrengthModel
from trade_snapshot.strength_calibration import (
    CalibrationMetadata,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
)
from trade_snapshot.trade_impact import prepare_season_baseline
from trade_snapshot.trade_space import TeamRoster, TradeCandidate, TradeConstraints, TradeSpace


POINTS = {"p1": 12.0, "p2": 8.0, "q1": 10.0, "q2": 6.0, "fa1": 9.0, "fa2": 4.0}


def model():
    return StrengthModel(
        (RoleDefinition("FLEX", RoleKind.STARTER, "FLEX", frozenset({"FLEX"})),),
        tuple(
            PlayerStrength(player, value, frozenset({"FLEX"}), {"FLEX": 0})
            for player, value in POINTS.items()
        ),
        50,
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


def rosters():
    return (
        TeamRoster("primary", ("p1", "p2"), 2, 2),
        TeamRoster("other", ("q1", "q2"), 2, 2),
    )


class RosterAdjustmentTests(unittest.TestCase):
    def test_imbalanced_trade_uses_optimal_drop_and_free_agent_replacement(self):
        value = model()
        primary, other = rosters()
        adjuster = PreparedRosterAdjuster(value, (primary, other))
        result = adjuster.adjust_trade(
            primary,
            other,
            TradeCandidate(("p1",), ("q1", "q2")),
        )
        self.assertEqual(result.primary.dropped_player_ids, ("p2",))
        self.assertEqual(set(result.primary.roster.player_ids), {"q1", "q2"})
        self.assertEqual(result.counterparty.added_player_ids, ("fa1",))
        self.assertEqual(set(result.counterparty.roster.player_ids), {"p1", "fa1"})

    def test_search_simulates_adds_and_drops_with_common_random_numbers(self):
        value = model()
        primary, other = rosters()
        adjuster = PreparedRosterAdjuster(value, (primary, other))
        pair = PreparedTradePair(value, primary, other, adjuster)
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=1,
            min_incoming=2,
            max_incoming=2,
            max_imbalance=1,
            require_no_drops=False,
        )
        space = TradeSpace(primary, other, constraints)
        projections = tuple(projection(player) for player in ("p1", "p2", "q1", "q2"))
        projections += tuple(
            type(projections[0])(
                canonical_player_id=player,
                snapshot_id="snapshot-1",
                scoring_profile_id="profile-1",
                season=2026,
                week=1,
                position="FLEX",
                status=projections[0].status,
                provider_observations=(
                    type(projections[0].provider_observations[0])(
                        "source", f"source-{player}", projections[0].status, POINTS[player], 1
                    ),
                ),
                minimum_observed_sources=1,
                position_stddev_floor=0,
                projected_fantasy_points=POINTS[player],
                between_provider_stddev=0,
                predictive_stddev=0,
                nfl_team_id=f"NFL-{player}",
                nfl_game_id="G1",
                opponent_team_id=f"OPP-{player}",
                is_home=True,
            )
            for player in ("fa1", "fa2")
        )
        eligibility = tuple(PlayerEligibility(player, ("FLEX",)) for player in POINTS)
        baseline = prepare_season_baseline(
            league_state(),
            (primary, other),
            projections,
            eligibility,
            CorrelatedScenarioConfig(5, 19, FactorLoadings(0, 0, 0, 1)),
        )
        runner = ResumableTradeSearch(
            space,
            pair,
            baseline,
            TradeSearchSettings(-1000, 1),
        )
        with TemporaryDirectory() as directory:
            outcome = runner.run(Path(directory) / "search.sqlite3")
            self.assertEqual(outcome.progress.total_candidate_count, 2)
            self.assertTrue(
                all(row.primary_dropped_player_ids for row in outcome.results)
            )
            self.assertTrue(
                all(
                    row.counterparty_added_player_ids == ("fa1",)
                    for row in outcome.results
                )
            )

    def test_drop_is_optimized_in_the_post_trade_depth_context(self):
        values = {
            "p1": (0, 20),
            "p2": (10, 5),
            "p3": (0, 0),
            "q1": (0, 30),
            "q2": (0, 0),
            "q3": (0, 0),
            "fa-residual": (10, 0),
            "fa-starter": (0, 15),
        }
        value = StrengthModel(
            (RoleDefinition("FLEX", RoleKind.STARTER, "FLEX", frozenset({"FLEX"})),),
            tuple(
                PlayerStrength(
                    player,
                    residual,
                    frozenset({"FLEX"}),
                    {"FLEX": assignment},
                )
                for player, (residual, assignment) in values.items()
            ),
            50,
            snapshot_id="snapshot-1",
            season=2026,
            scoring_profile_id="profile-1",
            calibration=model().calibration,
        )
        primary = TeamRoster("primary", ("p1", "p2", "p3"), 3, 3)
        other = TeamRoster("other", ("q1", "q2", "q3"), 3, 3)

        result = PreparedRosterAdjuster(value, (primary, other)).adjust_trade(
            primary,
            other,
            TradeCandidate(("p3",), ("q1", "q2")),
        )

        # Before the trade p2 has the lower marginal value. Once q1 takes the
        # starter role, p2's residual contribution makes p1 the correct drop.
        self.assertEqual(result.primary.dropped_player_ids, ("p1",))
        self.assertEqual(
            set(result.primary.roster.player_ids),
            {"p2", "q1", "q2"},
        )
        self.assertEqual(
            result.counterparty.added_player_ids,
            ("fa-starter",),
        )

    def test_ir_exemption_is_preserved_and_never_used_to_solve_active_overflow(self):
        value = model()
        primary = TeamRoster(
            "primary",
            ("p1", "p2", "fa2"),
            3,
            2,
            {"fa2"},
        )
        other = TeamRoster("other", ("q1", "q2"), 2, 2)
        adjuster = PreparedRosterAdjuster(value, (primary, other))

        result = adjuster.adjust_trade(
            primary,
            other,
            TradeCandidate(("p2",), ("q1", "q2")),
        )

        self.assertEqual(result.primary.dropped_player_ids, ("p1",))
        self.assertIn("fa2", result.primary.roster.player_ids)
        self.assertEqual(
            result.primary.roster.capacity_exempt_player_ids,
            frozenset({"fa2"}),
        )
        self.assertEqual(result.primary.roster.active_size, 2)

    def test_traded_ir_player_becomes_active_for_the_receiving_team(self):
        value = model()
        primary = TeamRoster(
            "primary",
            ("p1", "p2", "fa2"),
            3,
            2,
            {"fa2"},
        )
        other = TeamRoster("other", ("q1", "q2"), 2, 2)

        result = PreparedRosterAdjuster(value, (primary, other)).adjust_trade(
            primary,
            other,
            TradeCandidate(("fa2",), ("q1",)),
        )

        self.assertEqual(result.primary.roster.capacity_exempt_player_ids, frozenset())
        self.assertEqual(result.primary.roster.active_size, 2)
        self.assertEqual(result.primary.dropped_player_ids, ("p2",))
        self.assertEqual(result.counterparty.roster.capacity_exempt_player_ids, frozenset())
        self.assertEqual(result.counterparty.roster.active_size, 2)

    def test_existing_open_active_slot_accepts_net_incoming_player_without_drop(self):
        value = model()
        primary = TeamRoster("primary", ("p1", "p2"), 2, 3)
        other = TeamRoster("other", ("q1", "q2"), 2, 3)

        result = PreparedRosterAdjuster(value, (primary, other)).adjust_trade(
            primary,
            other,
            TradeCandidate(("p2",), ("q1", "q2")),
        )

        self.assertEqual(result.primary.dropped_player_ids, ())
        self.assertEqual(result.primary.roster.active_size, 3)
        self.assertEqual(
            set(result.primary.roster.player_ids),
            {"p1", "q1", "q2"},
        )


if __name__ == "__main__":
    unittest.main()
