import json
import unittest
from datetime import datetime, timedelta, timezone

from trade_snapshot.gm_timing_profile import build_completed_deal_timing_profiles
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryTeam,
    HistoryTimestampBasis,
    HistoryTransaction,
    HistoryTransactionAsset,
    HistoryTransactionKind,
    LeagueHistoryCapture,
    LeagueHistorySnapshot,
)
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


AS_OF = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
CAPTURED_AT = AS_OF - timedelta(minutes=10)
LEAGUE_KEY = "league_" + "1" * 64
BUNDLE_ID = "engine_" + "2" * 64


def league_state(results, *, include_completed=True, season=2026):
    completed = []
    wins = losses = ties = 0
    points_for = points_against = 0.0
    for week, result in enumerate(results, 1):
        if result == "win":
            left, right = 110.0, 100.0
            wins += 1
        elif result == "loss":
            left, right = 100.0, 110.0
            losses += 1
        else:
            left = right = 105.0
            ties += 1
        points_for += left
        points_against += right
        completed.append(CompletedFantasyMatchup(week, "a", "b", left, right))
    first_remaining = len(results) + 1
    return LeagueState(
        snapshot_id="snapshot",
        season=season,
        scoring_profile_id="scoring",
        first_remaining_week=first_remaining,
        teams=(LeagueTeam("a", "Alpha"), LeagueTeam("b", "Beta")),
        standings=(
            TeamStanding("a", wins, losses, ties, points_for, points_against),
            TeamStanding("b", losses, wins, ties, points_against, points_for),
        ),
        completed_matchups=tuple(completed) if include_completed else (),
        remaining_matchups=(FantasyMatchup(first_remaining, "a", "b"),),
        roster_rules=RosterRules(14, ("QB",)),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=first_remaining,
            playoff_weeks=(first_remaining + 1,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE,),
        ),
    )


def trade(name, week, *, basis=HistoryTimestampBasis.ESPN_PROPOSED_DATE, at=None):
    return HistoryTransaction(
        transaction_id=name,
        recorded_at=at or CAPTURED_AT - timedelta(days=1),
        timestamp_basis=basis,
        effective_week=week,
        kind=HistoryTransactionKind.TRADE,
        assets=(
            HistoryTransactionAsset(0, f"{name}-left", "a", "b"),
            HistoryTransactionAsset(1, f"{name}-right", "b", "a"),
        ),
    )


def waiver(name, week):
    return HistoryTransaction(
        transaction_id=name,
        recorded_at=CAPTURED_AT - timedelta(days=1),
        timestamp_basis=HistoryTimestampBasis.ESPN_PROPOSED_DATE,
        effective_week=week,
        kind=HistoryTransactionKind.WAIVER,
        assets=(HistoryTransactionAsset(0, f"{name}-player", None, "a"),),
    )


def capture(
    transactions=(),
    *,
    captured_at=CAPTURED_AT,
    complete=True,
):
    return LeagueHistoryCapture(
        league_key=LEAGUE_KEY,
        season=2026,
        captured_at=captured_at,
        coverage_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        coverage_end=captured_at,
        transaction_history_complete=complete,
        roster_complete=False,
        lineup_complete=False,
        teams=(HistoryTeam("a", "Alpha"), HistoryTeam("b", "Beta")),
        transactions=tuple(transactions),
        rosters=(),
    )


def history(*captures):
    binding = HistoryBundleBinding(LEAGUE_KEY, 2026, BUNDLE_ID, AS_OF)
    return LeagueHistorySnapshot(binding, (binding,), tuple(captures))


class CompletedDealTimingProfileTests(unittest.TestCase):
    def setUp(self):
        self.state = league_state(("win", "win", "loss", "loss", "loss", "win"))

    def test_no_history_returns_explicit_non_acceptance_profiles_for_every_team(self):
        result = build_completed_deal_timing_profiles(self.state, None)

        self.assertEqual(tuple(result), ("a", "b"))
        for profile in result.values():
            self.assertEqual(profile["status"], "unavailable")
            self.assertFalse(profile["manager_acceptance_modeled"])
            self.assertFalse(profile["use_for_personalization"])
            self.assertIsNone(profile["behavioral_label"])
            self.assertEqual(profile["coverage"]["status"], "not_collected")

    def test_first_week_has_no_elapsed_timing_period_to_estimate(self):
        state = league_state(())

        result = build_completed_deal_timing_profiles(
            state, history(capture())
        )["a"]

        self.assertEqual(result["coverage"]["elapsed_scoring_period_count"], 0)
        self.assertEqual(result["timing"]["status"], "unavailable")
        self.assertEqual(
            result["timing"]["reason"], "no_elapsed_scoring_periods"
        )

    def test_binary_windows_use_only_record_through_effective_week_minus_one(self):
        result = build_completed_deal_timing_profiles(
            self.state,
            history(capture((trade("one", 2), trade("two", 2), trade("three", 4)))),
        )["a"]
        timing = result["timing"]

        self.assertEqual(result["completed_trade_count"], 3)
        self.assertEqual(timing["active_scoring_period_count"], 2)
        self.assertEqual(timing["active_effective_weeks"], [2, 4])
        self.assertEqual(timing["rates"]["unconditional"]["exposures"], 6)
        self.assertEqual(timing["rates"]["unconditional"]["successes"], 2)
        self.assertAlmostEqual(
            timing["rates"]["unconditional"]["estimate"], 2.5 / 7
        )
        self.assertEqual(timing["rates"]["after_loss"]["exposures"], 3)
        self.assertEqual(timing["rates"]["after_loss"]["successes"], 1)
        self.assertEqual(timing["rates"]["after_nonloss"]["successes"], 1)
        self.assertEqual(timing["rates"]["downward"]["successes"], 1)
        self.assertEqual(
            result["observed_record_trajectory"][2]["record_slope_direction"],
            "downward",
        )
        self.assertFalse(result["manager_acceptance_modeled"])

    def test_fresh_complete_capture_enables_rates_but_health_blocks_labels(self):
        state = league_state(
            ("loss", "win", "loss", "win", "loss", "win", "loss", "win", "loss", "win", "loss")
        )
        result = build_completed_deal_timing_profiles(
            state,
            history(capture((trade("one", 2), trade("two", 4), trade("three", 6)))),
        )["a"]
        comparison = result["timing"]["comparisons"]["after_loss_minus_nonloss"]

        self.assertTrue(result["coverage"]["normalized_rates_available"])
        self.assertEqual(
            comparison["descriptive_separation"],
            "heuristic_80_bound_excludes_zero",
        )
        self.assertTrue(comparison["sample_gate_met"])
        self.assertEqual(comparison["confidence"], "heuristic_descriptive_only")
        self.assertIsNone(comparison["behavioral_label"])
        self.assertFalse(comparison["use_for_personalization"])
        self.assertEqual(
            comparison["suppressed_reason"],
            "historical_health_not_aligned_to_decision_windows",
        )

    def test_strong_gate_requires_95_percent_difference_but_remains_suppressed(self):
        state = league_state(
            ("loss", "win", "loss", "win", "loss", "win", "loss", "win", "loss", "win", "loss")
        )
        events = tuple(trade(f"trade-{week}", week) for week in (2, 4, 6, 8, 10))
        comparison = build_completed_deal_timing_profiles(
            state, history(capture(events))
        )["a"]["timing"]["comparisons"]["after_loss_minus_nonloss"]

        self.assertEqual(
            comparison["descriptive_separation"],
            "heuristic_95_bound_excludes_zero",
        )
        self.assertGreater(
            comparison["heuristic_difference_bound_95"]["lower"], 0
        )
        self.assertFalse(comparison["nominal_difference_coverage_claimed"])
        self.assertIsNone(comparison["behavioral_label"])
        self.assertFalse(comparison["use_for_personalization"])

    def test_non_trade_transactions_are_not_timing_events_and_ties_are_nonlosses(self):
        state = league_state(("tie", "loss", "win"))
        result = build_completed_deal_timing_profiles(
            state, history(capture((waiver("claim", 2), trade("deal", 2))))
        )["a"]

        self.assertEqual(result["completed_trade_count"], 1)
        self.assertEqual(result["timing"]["rates"]["after_nonloss"]["successes"], 1)

    def test_mixed_timestamp_bases_are_stratified_and_never_pooled(self):
        result = build_completed_deal_timing_profiles(
            self.state,
            history(
                capture(
                    (
                        trade("proposal", 2),
                        trade("execution", 4, basis=HistoryTimestampBasis.EXECUTED_AT),
                    )
                )
            ),
        )["a"]

        self.assertEqual(
            result["timestamp_bases"], ["espn_proposed_date", "executed_at"]
        )
        self.assertIsNone(result["selected_timestamp_basis"])
        self.assertEqual(result["timing"]["status"], "unavailable")
        self.assertEqual(
            result["timing"]["reason"], "mixed_timestamp_bases_are_not_pooled"
        )
        self.assertEqual(
            result["timing_by_timestamp_basis"]["espn_proposed_date"]["transaction_count"],
            1,
        )
        self.assertEqual(
            result["timing_by_timestamp_basis"]["executed_at"]["transaction_count"],
            1,
        )

    def test_stale_or_partial_coverage_keeps_counts_but_withholds_rates(self):
        stale_at = AS_OF - timedelta(hours=2)
        result = build_completed_deal_timing_profiles(
            self.state,
            history(capture((trade("one", 2, at=stale_at - timedelta(days=1)),), captured_at=stale_at)),
        )["a"]

        self.assertEqual(result["coverage"]["status"], "latest_capture_stale")
        rate = result["timing"]["rates"]["unconditional"]
        self.assertEqual(rate["successes"], 1)
        self.assertEqual(rate["exposures"], 6)
        self.assertIsNone(rate["estimate"])
        self.assertIsNone(rate["interval_95"])

        partial = build_completed_deal_timing_profiles(
            self.state,
            history(capture((trade("one", 2),), complete=False)),
        )["a"]
        self.assertEqual(partial["coverage"]["status"], "transaction_history_partial")
        self.assertIsNone(partial["timing"]["rates"]["unconditional"]["estimate"])

    def test_future_capture_and_transactions_are_ignored_at_requested_as_of(self):
        before = capture((trade("visible", 2),))
        future_at = AS_OF + timedelta(days=1)
        after = capture(
            (
                trade("visible", 2),
                trade("future", 4, at=future_at - timedelta(minutes=5)),
            ),
            captured_at=future_at,
        )
        result = build_completed_deal_timing_profiles(
            self.state, history(before, after)
        )["a"]

        self.assertEqual(result["completed_trade_count"], 1)
        self.assertEqual(result["timing"]["active_effective_weeks"], [2])
        self.assertEqual(result["coverage"]["latest_capture_id"], before.capture_id)

    def test_unusable_completed_matchups_prevent_normalized_context_rates(self):
        state = league_state(
            ("win", "win", "loss", "loss", "loss", "win"),
            include_completed=False,
        )
        result = build_completed_deal_timing_profiles(
            state, history(capture((trade("one", 2),)))
        )["a"]

        self.assertEqual(result["coverage"]["status"], "completed_matchups_unusable")
        self.assertEqual(result["observed_record_trajectory"], [])
        self.assertEqual(result["timing"]["rates"]["unconditional"]["successes"], 1)
        self.assertIsNone(result["timing"]["rates"]["unconditional"]["estimate"])

    def test_output_is_deterministic_finite_and_validates_identity(self):
        snapshot = history(capture((trade("one", 2),)))
        first = build_completed_deal_timing_profiles(self.state, snapshot)
        second = build_completed_deal_timing_profiles(self.state, snapshot)

        self.assertEqual(first, second)
        json.dumps(first, allow_nan=False, sort_keys=True)
        with self.assertRaisesRegex(ValueError, "season"):
            build_completed_deal_timing_profiles(
                league_state(("win",), season=2025), snapshot
            )
        with self.assertRaisesRegex(ValueError, "state"):
            build_completed_deal_timing_profiles(None, snapshot)


if __name__ == "__main__":
    unittest.main()
