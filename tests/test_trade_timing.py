import json
from datetime import timedelta
from types import SimpleNamespace
import unittest

from tests.test_delayed_trade_impact import _inputs as delayed_inputs
from tests.test_engine_bundle import engine_bundle
from tests.test_gm_trade_valuation import (
    LEAGUE_KEY,
    REQUEST_AT,
    capture as history_capture,
    current_bundle,
)
from trade_snapshot.delayed_trade_impact import prepare_delayed_baseline
from trade_snapshot.league_history import HistoryBundleBinding, LeagueHistorySnapshot
from trade_snapshot.roster_compatibility import RosterSwap, screened_roster_swaps
from trade_snapshot.season_trajectory import RecordTriggerScenarios
from trade_snapshot.trade_impact import prepare_season_baseline
from trade_snapshot._trade_timing_market import market_pattern, market_summary
from trade_snapshot._trade_timing_selection import build_recommendation, dominates
from trade_snapshot.trade_timing import (
    _evaluate_shortlist,
    _minimum_playoff_gain,
    _option_record,
    _current_injuries,
    _vulnerable_windows,
    build_trade_timing,
)


def _option(
    week,
    primary,
    partner,
    *,
    pressure=0.0,
    delay=0.0,
    mutual=True,
):
    return {
        "effective_week": week,
        "primary_playoff_probability_delta": primary,
        "partner_playoff_probability_delta": partner,
        "pressure_percentile": pressure,
        "delay_cost_primary": delay,
        "mutual_playoff_gain": mutual,
        "trigger": {"probability": pressure},
    }


def _impact(scenario_count, primary_delta, partner_delta):
    changes = {
        "primary": SimpleNamespace(
            playoff_probability_delta=primary_delta,
            expected_wins_delta=primary_delta,
        ),
        "other": SimpleNamespace(
            playoff_probability_delta=partner_delta,
            expected_wins_delta=partner_delta,
        ),
    }
    return SimpleNamespace(
        before=SimpleNamespace(scenario_count=scenario_count),
        for_team=changes.__getitem__,
    )


def _projected_week(week, pressure):
    return {
        "week": week,
        "opponent_id": "opponent",
        "opponent_name": "Opponent",
        "win_probability": 0.4,
        "loss_probability": 0.6,
        "downward_slope_probability": 0.5,
        "two_loss_streak_probability": 0.25,
        "playoff_probability_if_win": 0.55,
        "playoff_probability_if_loss": 0.35,
        "playoff_sensitivity": 0.2,
        "pressure_percentile": pressure,
    }


class TradeTimingTests(unittest.TestCase):
    def test_builds_deterministic_private_json_contract_without_acceptance_claim(self):
        bundle = engine_bundle()

        first = build_trade_timing(bundle, None, "primary", scenario_limit=3)
        second = build_trade_timing(bundle, None, "primary", scenario_limit=3)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["primary_team_id"], "primary")
        self.assertEqual(first["scenario_sampling"]["scenario_count"], 3)
        self.assertFalse(first["methodology"]["manager_acceptance_modeled"])
        self.assertFalse(first["trade_deadline"]["future_windows_are_legality_verified"])
        partner = first["partner_plans"][0]
        self.assertEqual(
            partner["candidate_screen"]["minimum_displayed_power_delta_each_team"],
            -5.0,
        )
        self.assertFalse(
            partner["completed_deal_timing"]["manager_acceptance_modeled"]
        )
        json.dumps(first, allow_nan=False, sort_keys=True)

    def test_roster_screen_honors_displayed_power_floor_and_limit(self):
        bundle = engine_bundle()

        all_swaps = screened_roster_swaps(
            bundle, "primary", "other", minimum_displayed_power_delta=-5
        )
        limited = screened_roster_swaps(
            bundle,
            "primary",
            "other",
            minimum_displayed_power_delta=-5,
            limit=2,
        )

        self.assertGreater(len(all_swaps), len(limited))
        self.assertEqual(limited, all_swaps[:2])
        self.assertTrue(
            all(
                row.primary_display_power_delta >= -5
                and row.counterparty_display_power_delta >= -5
                for row in all_swaps
            )
        )

    def test_default_plan_is_now_and_future_plan_stays_a_watch(self):
        now = _option(7, 0.04, 0.02)
        future = _option(9, 0.08, 0.03, pressure=0.9, delay=-0.04)

        result = build_recommendation((now, future), 7)

        self.assertIs(result["default_plan"], now)
        self.assertIs(result["conditional_watch_plan"], future)
        self.assertFalse(result["future_plan_is_recommendation"])
        self.assertEqual(result["future_plan_reason"], "league_trade_deadline_not_captured")

    def test_current_window_never_assigns_probability_to_unknown_legality(self):
        bundle = engine_bundle()
        swap = screened_roster_swaps(
            bundle, "primary", "other", minimum_displayed_power_delta=-5, limit=1
        )[0]
        first_week = bundle.state.first_remaining_week

        row = _option_record(
            bundle,
            swap,
            _impact(1_000, 0.01, 0.01),
            "primary",
            "other",
            first_week,
            first_week,
            None,
        )

        self.assertIsNone(row["trigger"]["probability"])
        self.assertEqual(
            row["trigger"]["probability_status"],
            "unmodeled_trade_legality",
        )

    def test_no_result_discloses_that_a_power_shortlist_was_not_exhaustive(self):
        result = build_recommendation(
            (_option(7, 0.01, -0.01, mutual=False),),
            7,
            shortlist_is_exhaustive=False,
        )

        self.assertEqual(
            result["status"], "no_mutual_gain_in_simulated_shortlist"
        )
        self.assertFalse(result["shortlist_is_exhaustive"])

    def test_materiality_floor_requires_two_scenario_steps(self):
        self.assertEqual(_minimum_playoff_gain(1_000), 0.0025)
        self.assertEqual(_minimum_playoff_gain(500), 0.004)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            _minimum_playoff_gain(0)

    def test_exact_two_scenario_step_gain_survives_float_rounding(self):
        bundle = engine_bundle()
        swap = screened_roster_swaps(
            bundle, "primary", "other", minimum_displayed_power_delta=-5, limit=1
        )[0]
        first_week = bundle.state.first_remaining_week
        exact_two_steps = 3 / 100 - 1 / 100

        row = _option_record(
            bundle,
            swap,
            _impact(100, exact_two_steps, exact_two_steps),
            "primary",
            "other",
            first_week,
            first_week,
            None,
        )

        self.assertTrue(row["mutual_playoff_gain"])

    def test_pareto_dominance_keeps_components_separate(self):
        strong = _option(7, 0.05, 0.03, pressure=0.8, delay=0)
        weak = _option(7, 0.03, 0.01, pressure=0.4, delay=0.02)
        tradeoff = _option(8, 0.08, 0.01, pressure=0.9, delay=0)

        self.assertTrue(dominates(strong, weak))
        self.assertFalse(dominates(strong, tradeoff))

    def test_projection_shape_is_explicitly_not_market_price(self):
        bundle = engine_bundle()
        swap = screened_roster_swaps(
            bundle, "primary", "other", minimum_displayed_power_delta=-5, limit=1
        )[0]

        pattern = market_pattern(bundle, swap, bundle.state.first_remaining_week)

        self.assertTrue(pattern["not_market_price_or_future_ecr"])
        self.assertIn("projected", pattern["summary"].casefold())

    def test_projection_shape_surfaces_each_partner_high_low_signal(self):
        buy_high = market_summary([], ["partner_buys_projected_high"])
        sell_low = market_summary([], ["partner_sells_projected_low"])

        self.assertIn("partner buys at a projected high", buy_high)
        self.assertIn("partner sells at a projected low", sell_low)

    def test_future_plan_uses_only_the_exact_pre_trade_trigger_paths(self):
        state, rosters, _, projections, eligibilities, config = delayed_inputs()
        baseline = prepare_season_baseline(
            state, rosters, projections, eligibilities, config
        )
        delayed_baseline = prepare_delayed_baseline(baseline)
        trigger = RecordTriggerScenarios((0, 2), 4, 4)
        window = {
            "result_week": 1,
            "effective_week": 2,
            "trigger_probability": 0.5,
            "trigger_scenario_count": 2,
            "pressure_percentile": 0.75,
            "conditional_trade_simulation_status": "ready",
        }
        bundle = SimpleNamespace(
            state=state,
            rosters=rosters,
            projections=projections,
            player_names={"p1": "Player One", "p2": "Player Two"},
        )
        swap = RosterSwap("p1", "p2", 0.0, 0.0, 0.0, 0.0)

        options = _evaluate_shortlist(
            bundle,
            delayed_baseline,
            "a",
            "b",
            (swap,),
            (window,),
            {("b", 1): trigger},
        )

        future = next(row for row in options if row["effective_week"] == 2)
        self.assertEqual(future["scenario_count"], 2)
        self.assertEqual(future["conditional_trigger_scenario_count"], 2)
        self.assertEqual(
            future["impact_scope"],
            "partner_loss_and_downward_slope_trigger_scenarios",
        )
        self.assertEqual(
            future["delay_comparison_scope"],
            "execute_now_vs_wait_within_same_pre_trade_trigger_scenarios",
        )

    def test_conditional_guard_rejects_an_unconditionally_positive_future(self):
        bundle = engine_bundle()
        swap = screened_roster_swaps(
            bundle, "primary", "other", minimum_displayed_power_delta=-5, limit=1
        )[0]
        first_week = bundle.state.first_remaining_week
        future_week = first_week + 1
        indexes = tuple(range(100))
        trigger = RecordTriggerScenarios(indexes, 1_000, 1_000)
        window = {
            "result_week": first_week,
            "effective_week": future_week,
            "trigger_probability": 0.1,
            "trigger_scenario_count": len(indexes),
            "pressure_percentile": 0.9,
            "conditional_trade_simulation_status": "ready",
        }

        class DelayedChange:
            def project(self, week):
                return _impact(1_000, 0.03, 0.03)

            def project_conditioned_many(self, weeks, scenario_indexes):
                self.selected_indexes = tuple(scenario_indexes)
                return {
                    first_week: _impact(100, 0.03, 0.03),
                    future_week: _impact(100, 0.03, -0.03),
                }

        class DelayedBaseline:
            def roster_change(self, after_rosters, affected_team_ids):
                self.change = DelayedChange()
                return self.change

        delayed_baseline = DelayedBaseline()
        options = _evaluate_shortlist(
            bundle,
            delayed_baseline,
            "primary",
            "other",
            (swap,),
            (window,),
            {("other", first_week): trigger},
        )
        recommendation = build_recommendation(options, first_week)
        future = next(
            row for row in options if row["effective_week"] == future_week
        )

        self.assertEqual(delayed_baseline.change.selected_indexes, indexes)
        self.assertFalse(future["mutual_playoff_point_estimate_gain"])
        self.assertIsNone(recommendation["conditional_watch_plan"])

    def test_future_trade_valuation_requires_one_hundred_trigger_paths(self):
        projected = tuple(
            _projected_week(week, pressure)
            for week, pressure in ((1, 0.8), (2, 0.7), (3, 0.6))
        )
        triggers = {
            ("other", 1): RecordTriggerScenarios(tuple(range(99)), 1_000, 1_000),
            ("other", 2): RecordTriggerScenarios(tuple(range(100)), 1_000, 1_000),
            ("other", 3): RecordTriggerScenarios(tuple(range(200)), 1_000, 1_000),
        }

        windows = _vulnerable_windows(projected, 1, "other", triggers)
        by_week = {row["result_week"]: row for row in windows}

        self.assertEqual(
            by_week[1]["conditional_trade_simulation_status"],
            "insufficient_trigger_scenarios",
        )
        self.assertEqual(
            by_week[2]["conditional_trade_simulation_status"], "ready"
        )
        self.assertEqual(by_week[2]["conditional_minimum_scenario_count"], 100)

    def test_invalid_primary_or_scenario_limit_fails_closed(self):
        bundle = engine_bundle()
        with self.assertRaisesRegex(ValueError, "primary_team_id"):
            build_trade_timing(bundle, None, "missing")
        with self.assertRaisesRegex(ValueError, "positive"):
            build_trade_timing(bundle, None, "primary", scenario_limit=0)

    def test_newer_incomplete_capture_does_not_mask_fresh_complete_health(self):
        requested = current_bundle(engine_bundle())
        binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        complete = history_capture(
            (),
            captured_at=REQUEST_AT - timedelta(minutes=5),
            injury_status="OUT",
        )
        incomplete = history_capture(
            (),
            captured_at=REQUEST_AT,
            complete=False,
        )
        snapshot = LeagueHistorySnapshot(
            binding, (binding,), (complete, incomplete)
        )

        injuries, status = _current_injuries(snapshot)

        self.assertEqual(set(injuries), {"p1", "p2", "q1", "q2"})
        self.assertEqual(status, "complete_and_fresh")

    def test_unrecognized_non_null_health_status_fails_closed(self):
        requested = current_bundle(engine_bundle())
        binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        unknown = history_capture((), injury_status="MYSTERY_STATUS")
        snapshot = LeagueHistorySnapshot(binding, (binding,), (unknown,))

        injuries, status = _current_injuries(snapshot)

        self.assertEqual(injuries, ())
        self.assertEqual(status, "partial_or_unrecognized_statuses")


if __name__ == "__main__":
    unittest.main()
