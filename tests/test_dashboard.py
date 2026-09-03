from dataclasses import replace
import json
import math
import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot.dashboard import (
    _field_conditioned_title_shares,
    build_league_dashboard,
)
from trade_snapshot.scenario_config import CorrelatedScenarioConfig, FactorLoadings
from trade_snapshot.trade_impact import prepare_season_baseline


def dashboard_inputs():
    bundle = engine_bundle()
    baseline = prepare_season_baseline(
        bundle.state,
        bundle.rosters,
        bundle.projections,
        bundle.eligibilities,
        bundle.scenario_config,
    )
    return bundle, baseline


def build_dashboard(bundle, baseline):
    return build_league_dashboard(
        bundle,
        baseline.season_projection,
        baseline.scenarios,
    )


class LeagueDashboardTests(unittest.TestCase):
    def test_builds_deterministic_strict_json_contract(self):
        bundle, baseline = dashboard_inputs()

        first = build_dashboard(bundle, baseline)
        second = build_dashboard(bundle, baseline)

        self.assertEqual(first, second)
        json.dumps(first, allow_nan=False)
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["bundle_id"], bundle.bundle_id)
        self.assertEqual(
            first["scenario_sampling"],
            {
                "bundle_scenario_count": 5,
                "dashboard_scenario_count": 5,
                "capped": False,
                "policy": "full_bundle_stream",
                "methodology": (
                    "Dashboard calculations use the bundle's complete scenario stream."
                ),
            },
        )
        self.assertEqual(first["weeks"], [1])
        self.assertEqual(first["positions"], ["FLEX"])
        self.assertEqual(first["power_engine_mode"], "holdout_validated")
        self.assertEqual(first["fantasypros_comparison"]["status"], "comparison_only")
        self.assertEqual(first["fantasypros_comparison"]["team_count"], 2)
        self.assertEqual(
            first["host_settlement_policy"]["status"],
            "partially_inferred",
        )
        self.assertEqual(
            first["championship_model"]["kind"],
            "field_conditioned_power_share_v1",
        )
        self.assertEqual(
            first["championship_model"]["status"], "modeled_estimate"
        )
        self.assertIn("not an exact", first["championship_model"]["limitations"])
        self.assertEqual(
            first["weekly_model"]["kind"],
            "mean_optimized_independent_scenarios_v1",
        )
        self.assertIn(
            "independent player-outcome",
            first["weekly_model"]["methodology"],
        )

        primary, other = first["teams"]
        self.assertEqual((primary["team_id"], other["team_id"]), ("primary", "other"))
        self.assertEqual((primary["power_rank"], other["power_rank"]), (1, 2))
        self.assertEqual((primary["power_score"], other["power_score"]), (100.0, 80.0))
        self.assertEqual(primary["weekly_outlook"][0]["projected_points"], 12.0)
        self.assertEqual(primary["weekly_outlook"][0]["uncertainty"], 0.0)
        self.assertEqual(primary["weekly_outlook"][0]["opponent_id"], "other")
        self.assertEqual(primary["weekly_outlook"][0]["matchup_win_probability"], 1.0)
        self.assertEqual(other["weekly_outlook"][0]["matchup_win_probability"], 0.0)
        self.assertEqual(primary["schedule_difficulty_rank"], 2)
        self.assertEqual(other["schedule_difficulty_rank"], 1)
        self.assertEqual(primary["position_outlook"][0]["projected_points"], 20.0)
        self.assertEqual(primary["position_outlook"][0]["league_percentile"], 1.0)
        self.assertEqual(other["position_outlook"][0]["league_percentile"], 0.0)
        self.assertEqual(
            primary["fantasypros_comparison"]["source"]["current_rank"],
            next(
                row.current_rank
                for row in bundle.fantasypros_benchmark.teams
                if row.team_id == primary["team_id"]
            ),
        )
        self.assertAlmostEqual(
            primary["fantasypros_comparison"]["local_minus_source"][
                "playoff_probability"
            ],
            primary["playoff_probability"]
            - primary["fantasypros_comparison"]["source"][
                "playoff_probability"
            ],
        )

    def test_surfaces_current_rank_drift_without_using_benchmark_as_input(self):
        bundle, baseline = dashboard_inputs()
        benchmark = bundle.fantasypros_benchmark
        swapped = replace(
            benchmark,
            teams=tuple(
                replace(
                    row,
                    current_rank=(2.0 if row.current_rank == 1 else 1.0),
                )
                for row in benchmark.teams
            ),
        )
        changed_bundle = replace(bundle, fantasypros_benchmark=swapped)

        original = build_dashboard(bundle, baseline)
        changed = build_dashboard(changed_bundle, baseline)

        self.assertEqual(
            [row["current_rank"] for row in changed["teams"]],
            [row["current_rank"] for row in original["teams"]],
        )
        self.assertFalse(changed["fantasypros_comparison"]["current_rank_all_match"])
        self.assertFalse(
            changed["host_settlement_policy"][
                "current_rank_matches_fantasypros"
            ]
        )
    def test_weekly_model_discloses_nonzero_shared_factors(self):
        bundle, _ = dashboard_inputs()
        correlated = replace(
            bundle,
            scenario_config=CorrelatedScenarioConfig(
                bundle.scenario_config.scenario_count,
                bundle.scenario_config.seed,
                FactorLoadings(0, 1, 0, 0),
            ),
        )
        baseline = prepare_season_baseline(
            correlated.state,
            correlated.rosters,
            correlated.projections,
            correlated.eligibilities,
            correlated.scenario_config,
        )

        result = build_dashboard(correlated, baseline)

        self.assertEqual(
            result["weekly_model"]["kind"],
            "mean_optimized_correlated_scenarios_v1",
        )
        self.assertIn(
            "shared league/game/team-factor",
            result["weekly_model"]["methodology"],
        )

    def test_probability_and_distribution_invariants_hold(self):
        bundle, baseline = dashboard_inputs()
        result = build_dashboard(bundle, baseline)
        teams = result["teams"]

        self.assertAlmostEqual(
            math.fsum(row["playoff_probability"] for row in teams),
            result["playoff_team_count"],
        )
        self.assertAlmostEqual(
            math.fsum(row["championship_probability"] for row in teams), 1.0
        )
        for row in teams:
            with self.subTest(team_id=row["team_id"]):
                self.assertLessEqual(
                    row["championship_probability"], row["playoff_probability"]
                )
                self.assertAlmostEqual(math.fsum(row["rank_distribution"]), 1.0)
                self.assertAlmostEqual(
                    math.fsum(row["seed_distribution"]),
                    row["playoff_probability"],
                )
                projected_remaining = math.fsum(
                    week["projected_points"] for week in row["weekly_outlook"]
                )
                self.assertAlmostEqual(
                    row["current_points_for"] + projected_remaining,
                    row["expected_final_points_for"],
                )

        left, right = (row["weekly_outlook"][0] for row in teams)
        self.assertAlmostEqual(
            left["matchup_win_probability"] + right["matchup_win_probability"],
            1.0,
        )

    def test_score_adjustments_are_included_in_weekly_outlook(self):
        bundle, _ = dashboard_inputs()
        matchup = replace(bundle.state.remaining_matchups[0], team1_score_adjustment=1.25)
        adjusted = replace(
            bundle,
            state=replace(bundle.state, remaining_matchups=(matchup,)),
        )
        baseline = prepare_season_baseline(
            adjusted.state,
            adjusted.rosters,
            adjusted.projections,
            adjusted.eligibilities,
            adjusted.scenario_config,
        )

        result = build_dashboard(adjusted, baseline)
        primary = next(row for row in result["teams"] if row["team_id"] == "primary")

        self.assertEqual(primary["weekly_outlook"][0]["projected_points"], 13.25)
        self.assertEqual(primary["expected_final_points_for"], 13.25)

    def test_field_conditioned_title_proxy_has_known_power_weighting(self):
        powers = {"a": 100.0, "b": 90.0, "c": 80.0, "d": 80.0}
        first = _field_conditioned_title_shares(("a", "b"), powers)
        second = _field_conditioned_title_shares(("d", "c"), powers)
        averaged = {
            team_id: (first.get(team_id, 0.0) + second.get(team_id, 0.0)) / 2
            for team_id in powers
        }

        self.assertAlmostEqual(averaged["a"], 1 / 3)
        self.assertAlmostEqual(averaged["b"], 1 / 6)
        self.assertAlmostEqual(averaged["c"], 1 / 4)
        self.assertAlmostEqual(averaged["d"], 1 / 4)
        self.assertAlmostEqual(math.fsum(averaged.values()), 1.0)
        self.assertTrue(all(value <= 0.5 for value in averaged.values()))

    def test_rejects_detached_or_malformed_baseline_projection(self):
        bundle, baseline = dashboard_inputs()
        projection = baseline.season_projection
        primary = projection.teams[0]
        invalid = (
            replace(projection, snapshot_id="other-snapshot"),
            replace(projection, scenario_count=projection.scenario_count + 1),
            replace(
                projection,
                teams=(
                    replace(primary, rank_distribution=(math.nan, 0.0)),
                    projection.teams[1],
                ),
            ),
            replace(
                projection,
                teams=(
                    replace(primary, seed_distribution=(0.5,)),
                    projection.teams[1],
                ),
            ),
        )
        for projection in invalid:
            with self.subTest(projection=projection):
                with self.assertRaises(ValueError):
                    build_league_dashboard(bundle, projection, baseline.scenarios)


if __name__ == "__main__":
    unittest.main()
