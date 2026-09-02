from datetime import datetime, timezone
import math
from pathlib import Path
from tempfile import TemporaryDirectory
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
from trade_snapshot.scenario_config import (
    CorrelatedScenarioConfig,
    FactorLoadings,
    PlayerEligibility,
)
from trade_snapshot.search import PreparedTradePair
from trade_snapshot.search_runner import (
    ResumableTradeSearch,
    TradeSearchProgress,
    TradeSearchSettings,
)
from trade_snapshot.strength import (
    CalibrationMetadata,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
    StrengthModel,
)
from trade_snapshot.trade_impact import prepare_season_baseline
from trade_snapshot.trade_filters import TradeFilterMode, TradePackageFilter
from trade_snapshot.trade_space import TeamRoster, TradeConstraints, TradeSpace


PLAYER_POINTS = {"p1": 12.0, "p2": 8.0, "q1": 10.0, "q2": 6.0}


def league_state(scoring_profile_id="profile-1"):
    return LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id=scoring_profile_id,
        first_remaining_week=1,
        teams=(LeagueTeam("primary", "Primary"), LeagueTeam("other", "Other")),
        standings=(
            TeamStanding("primary", 0, 0, 0, 0, 0),
            TeamStanding("other", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(FantasyMatchup(1, "primary", "other"),),
        roster_rules=RosterRules(2, ("FLEX",)),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=1,
            playoff_weeks=(2,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def projection(player_id, scoring_profile_id="profile-1"):
    points = PLAYER_POINTS[player_id]
    return EnsembleProjection(
        canonical_player_id=player_id,
        snapshot_id="snapshot-1",
        scoring_profile_id=scoring_profile_id,
        season=2026,
        week=1,
        position="FLEX",
        status=ProjectionStatus.OBSERVED,
        provider_observations=(
            ProviderObservation(
                "source", f"source-{player_id}", ProjectionStatus.OBSERVED, points, 1
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


def strength_model(scoring_profile_id="profile-1"):
    return StrengthModel(
        role_definitions=(
            RoleDefinition("FLEX", RoleKind.STARTER, "FLEX", frozenset({"FLEX"})),
        ),
        players=tuple(
            PlayerStrength(player_id, points, frozenset({"FLEX"}), {"FLEX": 0})
            for player_id, points in PLAYER_POINTS.items()
        ),
        normalization_denominator=40,
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id=scoring_profile_id,
        calibration=CalibrationMetadata(
            analyzer_bundle_url="https://cdn.fantasypros.com/assets/trade-analyzer.js",
            analyzer_bundle_sha256="1" * 64,
            response_schema_sha256="2" * 64,
            captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
    )


def components(
    *,
    reverse_space=False,
    threshold=-100.0,
    checkpoint_interval=2,
    scoring_profile_id="profile-1",
    primary_exempt=frozenset(),
    trade_constraints=None,
):
    primary = TeamRoster("primary", ("p1", "p2"), 2, 2, primary_exempt)
    other = TeamRoster("other", ("q1", "q2"), 2, 2)
    rosters = (primary, other)
    model = strength_model(scoring_profile_id)
    pair = PreparedTradePair(model, primary, other)
    space_primary = TeamRoster(
        "primary",
        tuple(reversed(primary.player_ids)) if reverse_space else primary.player_ids,
        2,
        2,
        primary_exempt,
    )
    space_other = TeamRoster(
        "other", tuple(reversed(other.player_ids)) if reverse_space else other.player_ids, 2, 2
    )
    space = TradeSpace(
        space_primary,
        space_other,
        trade_constraints or TradeConstraints(require_no_drops=True),
    )
    projections = tuple(
        projection(player_id, scoring_profile_id) for player_id in PLAYER_POINTS
    )
    eligibility = tuple(
        PlayerEligibility(player_id, ("FLEX",)) for player_id in PLAYER_POINTS
    )
    baseline = prepare_season_baseline(
        league_state(scoring_profile_id),
        rosters,
        projections,
        eligibility,
        CorrelatedScenarioConfig(5, 19, FactorLoadings(0, 0, 0, 1)),
    )
    runner = ResumableTradeSearch(
        space,
        pair,
        baseline,
        TradeSearchSettings(threshold, checkpoint_interval),
    )
    return runner


class ResumableTradeSearchTests(unittest.TestCase):
    def test_active_package_filter_is_bound_into_checkpoint_identity(self):
        unfiltered = components()
        filtered = components(
            trade_constraints=TradeConstraints(
                require_no_drops=True,
                outgoing_filter=TradePackageFilter(
                    frozenset({"p1"}), TradeFilterMode.INCLUDE
                ),
            )
        )

        unfiltered_record = unfiltered.run_definition.trade_constraint_record[
            "trade_constraints"
        ]
        filtered_record = filtered.run_definition.trade_constraint_record[
            "trade_constraints"
        ]
        self.assertNotEqual(unfiltered.run_definition.run_id, filtered.run_definition.run_id)
        self.assertNotIn("outgoing_filter", unfiltered_record)
        self.assertEqual(filtered_record["outgoing_filter"]["player_ids"], ("p1",))
        self.assertEqual(filtered_record["package_filter_semantics_version"], 1)

    def test_runs_all_candidates_locally_and_resuming_is_idempotent(self):
        runner = components()
        progress_updates = []
        with TemporaryDirectory() as directory:
            database = Path(directory) / "search.sqlite3"
            first = runner.run(database, on_progress=progress_updates.append)
            second = runner.run(database)

        self.assertEqual(first.progress.next_candidate_index, 4)
        self.assertEqual(first.progress.total_candidate_count, 4)
        self.assertEqual(first.progress.completion_fraction, 1)
        self.assertEqual(first.progress.power_qualified_count, 4)
        self.assertEqual(first.progress.playoff_evaluated_count, 4)
        self.assertFalse(first.progress.cancelled)
        self.assertEqual(len(first.results), 4)
        self.assertEqual(first, second)
        self.assertEqual(progress_updates[-1], first.progress)
        self.assertTrue(
            all(row.primary_playoff_before is not None for row in first.results)
        )

    def test_cancel_checkpoint_resumes_from_exact_next_candidate(self):
        runner = components(checkpoint_interval=100)
        calls = 0

        def cancel_after_two():
            nonlocal calls
            calls += 1
            return calls > 2

        with TemporaryDirectory() as directory:
            database = Path(directory) / "search.sqlite3"
            partial = runner.run(database, should_cancel=cancel_after_two)
            complete = runner.run(database)

        self.assertTrue(partial.progress.cancelled)
        self.assertEqual(partial.progress.next_candidate_index, 2)
        self.assertEqual(tuple(row.candidate_index for row in partial.results), (0, 1))
        self.assertFalse(complete.progress.cancelled)
        self.assertEqual(complete.progress.next_candidate_index, 4)
        self.assertEqual(tuple(row.candidate_index for row in complete.results), (0, 1, 2, 3))

    def test_power_threshold_avoids_every_playoff_simulation(self):
        runner = components(threshold=1000)
        with TemporaryDirectory() as directory:
            outcome = runner.run(Path(directory) / "search.sqlite3")

        self.assertEqual(outcome.progress.next_candidate_index, 4)
        self.assertEqual(outcome.progress.power_qualified_count, 0)
        self.assertEqual(outcome.progress.playoff_evaluated_count, 0)
        self.assertEqual(outcome.results, ())

    def test_semantically_equal_rosters_accept_different_input_order_safely(self):
        original = components()
        reordered = components(reverse_space=True)

        self.assertNotEqual(original.run_definition.run_id, reordered.run_definition.run_id)
        with TemporaryDirectory() as directory:
            outcome = reordered.run(Path(directory) / "search.sqlite3")
        self.assertEqual(outcome.progress.next_candidate_index, 4)

    def test_capacity_exempt_membership_changes_the_resumable_run_identity(self):
        ordinary = components()
        with_ir = components(primary_exempt={"p2"})

        self.assertNotEqual(
            ordinary.season_baseline.scenarios.run_id,
            with_ir.season_baseline.scenarios.run_id,
        )
        self.assertNotEqual(
            ordinary.run_definition.run_id,
            with_ir.run_definition.run_id,
        )

    def test_rejects_strength_model_from_another_engine_identity(self):
        runner = components()
        original = runner.prepared_strength.model
        changed = StrengthModel(
            original.role_definitions,
            original.players.values(),
            original.normalization_denominator,
            snapshot_id=original.snapshot_id,
            season=original.season + 1,
            scoring_profile_id=original.scoring_profile_id,
            calibration=original.calibration,
        )
        pair = PreparedTradePair(
            changed,
            runner.prepared_strength.primary,
            runner.prepared_strength.counterparty,
        )

        with self.assertRaisesRegex(ValueError, "engine identity"):
            ResumableTradeSearch(
                runner.trade_space,
                pair,
                runner.season_baseline,
                runner.settings,
            )

    def test_settings_and_progress_reject_invalid_numbers(self):
        for value in (math.nan, math.inf, -math.inf, True, "-5"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite number"):
                    TradeSearchSettings(value)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            TradeSearchProgress("run", 2, 1, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
