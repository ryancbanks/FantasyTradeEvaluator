from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import unittest

from tests.test_engine_bundle import (
    engine_bundle,
    nfl_schedule_for,
    ros_derived_bundle,
)
from tests.test_feature_engineering import inputs
from tests.source_fixtures import projection_source_manifest
from tests.test_weekly_engine import (
    SCORING_PROFILE,
    build as build_weekly_bundle,
    raw_rows,
)
from trade_snapshot._app_support import bundle_summary
from trade_snapshot.data_readiness import (
    build_bundle_data_readiness,
    build_data_readiness_snapshot,
)
from trade_snapshot.nfl_schedule import NflSchedule, NflTeamWeekStatus
from trade_snapshot.league_state import Tiebreaker
from trade_snapshot.projections import (
    ProviderStatusObservation,
    ProviderStatusScope,
    RemainingSeasonProjection,
    WeeklyProjection,
)


def _timestamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class BundleDataReadinessTests(unittest.TestCase):
    def test_reports_feature_contracts_and_evidence_coverage(self):
        bundle = engine_bundle()

        report = build_bundle_data_readiness(bundle)

        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["status"], "ready_with_known_limitations")
        self.assertEqual(
            {
                key: value
                for key, value in report["bound_inputs"].items()
                if key != "projection_source_manifest"
            },
            {
                "league_binding": {
                    "league_binding_id": (
                        bundle.source_manifest.league_binding_id
                    ),
                    "league_binding_scope": (
                        bundle.source_manifest.league_binding_scope.value
                    ),
                    "host_provider": bundle.source_manifest.host_provider,
                    "host_snapshot_id": (
                        bundle.source_manifest.host_snapshot_id
                    ),
                    "host_captured_at": (
                        bundle.source_manifest.host_captured_at.isoformat(
                            timespec="microseconds"
                        ).replace("+00:00", "Z")
                    ),
                    "fantasypros_league_artifact_id": (
                        bundle.source_manifest.fantasypros_league_artifact_id
                    ),
                    "fantasypros_captured_at": (
                        bundle.source_manifest.fantasypros_captured_at.isoformat(
                            timespec="microseconds"
                        ).replace("+00:00", "Z")
                    ),
                    "completed_history_available": (
                        bundle.source_manifest.completed_history_available
                    ),
                },
                "fantasypros_comparison_benchmark": {
                    "benchmark_id": (
                        bundle.fantasypros_benchmark.benchmark_id
                    ),
                    "source_artifact_id": (
                        bundle.fantasypros_benchmark.source_artifact_id
                    ),
                    "captured_at": _timestamp(
                        bundle.fantasypros_benchmark.captured_at
                    ),
                },
                "scoring_profile_id": bundle.scoring_profile.scoring_profile_id,
                "nfl_schedule_id": bundle.nfl_schedule.schedule_id,
                "nfl_schedule_source_provider": (
                    bundle.nfl_schedule.source_provider
                ),
                "nfl_schedule_captured_at": (
                    bundle.nfl_schedule.captured_at.isoformat(
                        timespec="microseconds"
                    ).replace("+00:00", "Z")
                ),
                "ensemble_config_id": bundle.ensemble_config.config_id,
                "strength_formula_id": bundle.strength_formula.formula_id,
                "strength_model_id": bundle.strength_model.model_id,
                "scenario_player_score_floor": (
                    bundle.scenario_config.player_score_floor
                ),
                "ecr_snapshot_ids": {
                    row.period.value: row.ecr_id for row in bundle.ecr_snapshots
                },
            },
        )
        source_binding = report["bound_inputs"]["projection_source_manifest"]
        self.assertEqual(
            source_binding["manifest_id"],
            bundle.projection_source_manifest.manifest_id,
        )
        self.assertEqual(
            source_binding["evaluation_scoring_profile_id"],
            bundle.scoring_profile.scoring_profile_id,
        )
        self.assertEqual(
            {row["point_basis"] for row in source_binding["sources"]},
            {"provider_total"},
        )
        self.assertEqual(
            {row["host_scoring_compatibility"] for row in source_binding["sources"]},
            {"base_format_only"},
        )
        self.assertEqual(
            report["calculation_domain"]["ensemble_player_week_count"],
            len(bundle.projections),
        )
        self.assertEqual(
            report["coverage"]["full_horizon_provider_cells"],
            len(bundle.strength_model.players),
        )
        self.assertEqual(
            report["coverage"]["available_full_horizon_provider_cells"],
            len(bundle.strength_model.players),
        )
        self.assertEqual(
            report["coverage"]["unavailable_full_horizon_provider_cells"],
            0,
        )
        self.assertEqual(
            report["coverage"]["available_full_horizon_ensemble_players"],
            len(bundle.strength_model.players),
        )
        self.assertGreaterEqual(
            report["coverage"]["source_capture_timestamp_count"],
            3,
        )
        self.assertGreaterEqual(report["coverage"]["capture_window_seconds"], 0)
        self.assertNotIn("source_league_id", str(report))
        self.assertEqual(
            report["coverage"]["fantasypros_comparison_team_count"],
            len(bundle.state.teams),
        )
        source_coverage = report["coverage"]["projection_sources"]
        self.assertEqual(
            source_coverage["manifest_id"],
            bundle.projection_source_manifest.manifest_id,
        )
        self.assertEqual(
            source_coverage["provider_total_sources"],
            len(bundle.projection_source_manifest.sources),
        )
        self.assertEqual(source_coverage["locally_recomputed_sources"], 0)
        self.assertEqual(source_coverage["base_format_only_sources"], len(
            bundle.projection_source_manifest.sources
        ))
        self.assertEqual(source_coverage["exact_host_rules_sources"], 0)
        self.assertEqual(
            sum(
                values["captured_attempts"]
                for values in source_coverage["providers"].values()
            ),
            len(bundle.projection_source_manifest.attempts),
        )
        self.assertEqual(
            report["capabilities"]["fantasypros_comparison_benchmark"]["status"],
            "comparison_only",
        )
        self.assertIn(
            "never blended into local",
            report["capabilities"]["fantasypros_comparison_benchmark"][
                "limitations"
            ][0],
        )
        self.assertTrue(
            any(
                "not calibrated probabilities" in limitation
                for limitation in report["capabilities"][
                    "playoff_model_estimates"
                ]["limitations"]
            )
        )
        self.assertEqual(
            report["capabilities"]["expected_standings"]["status"],
            "ready_with_limitations",
        )
        self.assertEqual(
            report["capabilities"]["playoff_model_estimates"]["status"],
            "model_estimate_with_limitations",
        )
        self.assertTrue(
            report["capabilities"]["fantasypros_style_power"]["evidence"][
                "required_projection_evidence_complete"
            ]
        )
        self.assertEqual(
            report["capabilities"]["fantasypros_style_power"]["status"],
            "ready_with_holdout_validated_scope",
        )
        self.assertEqual(
            report["capabilities"]["fantasypros_style_power"][
                "holdout_validated_scope"
            ],
            {
                "balanced_package_sizes": [1, 2, 3, 4],
                "roster_adjustments": False,
            },
        )
        self.assertEqual(
            report["capabilities"]["exact_championship_simulation"]["status"],
            "not_ready",
        )
        self.assertTrue(
            any(
                "tiebreak settlement" in limitation
                for limitation in report["capabilities"]["expected_standings"][
                    "limitations"
                ]
            )
        )
        self.assertNotIn(
            "portable NFL playoff-week schedule evidence",
            report["capabilities"]["exact_championship_simulation"]["missing"],
        )
        self.assertEqual(
            {row["data"] for row in report["missing_data_plan"]},
            {
                "exact_projection_scoring_compatibility",
                "player_week_availability",
                "postseason_schedule_and_bracket",
                "calibrated_outcome_correlation",
                "history_profiles_and_draft",
            },
        )

    def test_bundle_catalog_summary_carries_the_same_report(self):
        bundle = engine_bundle()

        summary = bundle_summary(bundle)
        self.assertEqual(summary["league_key"], "1" * 12)
        self.assertEqual(summary["league_label"], f"ESPN workspace {'1' * 12}")
        self.assertEqual(
            summary["data_readiness"],
            build_bundle_data_readiness(bundle),
        )
        self.assertEqual(
            summary["status"],
            "not_ready"
            if summary["data_readiness"]["status"] == "not_ready"
            else "ready",
        )

    def test_tiebreaker_requirements_gate_every_season_consumer(self):
        bundle = engine_bundle()
        state = replace(
            bundle.state,
            playoff_rules=replace(
                bundle.state.playoff_rules,
                tiebreaker_order=(
                    Tiebreaker.WIN_PERCENTAGE,
                    Tiebreaker.HEAD_TO_HEAD,
                ),
                head_to_head_policy=None,
            ),
        )
        report = build_bundle_data_readiness(replace(bundle, state=state))

        for capability in (
            "expected_standings",
            "playoff_model_estimates",
            "trade_search",
            "team_outlook_and_exports",
        ):
            with self.subTest(capability=capability):
                self.assertEqual(
                    report["capabilities"][capability]["status"],
                    "not_ready",
                )
        for capability in (
            "expected_standings",
            "playoff_model_estimates",
            "team_outlook_and_exports",
        ):
            self.assertTrue(
                any(
                    "head_to_head_policy" in reason
                    for reason in report["capabilities"][capability]["missing"]
                )
            )
        self.assertFalse(
            report["capabilities"]["expected_standings"]["evidence"][
                "tiebreaker_inputs_ready"
            ]
        )

    def test_capture_window_includes_host_and_fantasypros_league_sources(self):
        bundle = engine_bundle()
        earlier = bundle.nfl_schedule.captured_at - timedelta(days=2)
        later = bundle.nfl_schedule.captured_at + timedelta(days=3)
        benchmark_later = bundle.nfl_schedule.captured_at + timedelta(days=4)
        manifest = replace(
            bundle.source_manifest,
            host_captured_at=earlier,
            fantasypros_captured_at=later,
        )
        benchmark = replace(
            bundle.fantasypros_benchmark,
            captured_at=benchmark_later,
        )

        coverage = build_bundle_data_readiness(
            replace(
                bundle,
                source_manifest=manifest,
                fantasypros_benchmark=benchmark,
            )
        )["coverage"]

        self.assertEqual(coverage["earliest_capture_at"], _timestamp(earlier))
        self.assertEqual(
            coverage["latest_capture_at"],
            _timestamp(benchmark_later),
        )
        self.assertEqual(coverage["capture_window_seconds"], 6 * 24 * 60 * 60)

    def test_freezes_export_coverage_and_discloses_missing_kickoff_times(self):
        bundle = engine_bundle()

        snapshot = build_data_readiness_snapshot(bundle)

        self.assertEqual(
            snapshot.provider_cell_count,
            len(bundle.projections),
        )
        self.assertEqual(
            snapshot.provider_cell_count,
            snapshot.direct_provider_cells
            + snapshot.ros_derived_provider_cells
            + snapshot.schedule_derived_availability_cells
            + snapshot.unavailable_provider_cells
            + snapshot.unattributed_provider_cells,
        )
        self.assertGreater(snapshot.first_week_scheduled_games, 0)
        self.assertEqual(
            snapshot.first_week_games_missing_kickoff,
            snapshot.first_week_scheduled_games,
        )
        self.assertIn("partially played weeks", snapshot.as_of_time_limitation)
        self.assertEqual(
            snapshot.scenario_player_score_floor,
            bundle.scenario_config.player_score_floor,
        )
        self.assertEqual(snapshot.expected_standings_status, "ready_with_limitations")
        self.assertEqual(
            snapshot.playoff_model_status,
            "model_estimate_with_limitations",
        )
        self.assertIn(
            "proof-bound contract",
            snapshot.host_settlement_policy_limitation,
        )
        self.assertTrue(
            any(
                "partially played weeks" in limitation
                for limitation in build_bundle_data_readiness(bundle)["capabilities"][
                    "team_outlook_and_exports"
                ]["limitations"]
            )
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.direct_provider_cells = 0

    def test_omits_as_of_limitation_when_every_first_week_kickoff_is_known(self):
        bundle = engine_bundle()
        kickoff = bundle.nfl_schedule.captured_at + timedelta(days=1)
        schedule = NflSchedule(
            bundle.nfl_schedule.season,
            bundle.nfl_schedule.captured_at,
            bundle.nfl_schedule.source_provider,
            tuple(
                replace(row, kickoff_at=kickoff)
                if row.status is NflTeamWeekStatus.SCHEDULED
                else row
                for row in bundle.nfl_schedule.team_weeks
            ),
        )

        snapshot = build_data_readiness_snapshot(
            replace(bundle, nfl_schedule=schedule)
        )

        self.assertEqual(snapshot.first_week_games_missing_kickoff, 0)
        self.assertIsNone(snapshot.as_of_time_limitation)

    def test_coverage_uses_validated_ros_lineage(self):
        bundle = ros_derived_bundle()

        report = build_bundle_data_readiness(bundle)
        coverage = report["coverage"]
        snapshot = build_data_readiness_snapshot(bundle)

        self.assertEqual(coverage["ros_derived_provider_cells"], 1)
        self.assertEqual(coverage["unattributed_provider_cells"], 0)
        self.assertIn("divided evenly", snapshot.ros_allocation_limitation)
        self.assertTrue(
            any(
                "not provider-published matchup projections" in limitation
                for limitation in report["capabilities"]["player_lab"]["limitations"]
            )
        )

    def test_keeps_custom_scoring_caveat_for_supported_provider_totals(self):
        bundle = engine_bundle()
        report = build_bundle_data_readiness(bundle)
        snapshot = build_data_readiness_snapshot(bundle)

        self.assertEqual(
            report["coverage"]["projection_sources"][
                "provider_total_sources"
            ],
            len(bundle.projection_source_manifest.sources),
        )
        self.assertTrue(
            any(
                "custom host rule" in limitation
                for limitation in report["capabilities"]["expected_standings"][
                    "limitations"
                ]
            )
        )
        self.assertIsNotNone(snapshot.custom_scoring_limitation)

    def test_playoff_ros_coverage_excludes_a_verified_bye_from_required_weeks(self):
        bundle = engine_bundle()
        p1 = next(
            row for row in bundle.projections if row.canonical_player_id == "p1"
        )
        schedule = nfl_schedule_for(bundle.projections)
        schedule = NflSchedule(
            schedule.season,
            schedule.captured_at,
            schedule.source_provider,
            tuple(
                replace(
                    row,
                    status=NflTeamWeekStatus.BYE,
                    nfl_game_id=None,
                    opponent_team_id=None,
                    is_home=None,
                )
                if row.week == 2
                and row.nfl_team_id in {p1.nfl_team_id, p1.opponent_team_id}
                else row
                for row in schedule.team_weeks
            ),
        )
        active_weeks = {
            player_id: tuple(
                row.week
                for row in schedule.team_weeks
                if row.nfl_team_id == projection.nfl_team_id
                and row.status is NflTeamWeekStatus.SCHEDULED
            )
            for player_id, projection in {
                row.canonical_player_id: row for row in bundle.projections
            }.items()
        }
        evidence = tuple(
            replace(row, applicable_weeks=active_weeks[row.canonical_player_id])
            if isinstance(row, RemainingSeasonProjection)
            else row
            for row in bundle.projection_evidence
        )

        coverage = build_bundle_data_readiness(
            replace(
                bundle,
                nfl_schedule=schedule,
                projection_evidence=evidence,
                projection_source_manifest=projection_source_manifest(evidence),
            )
        )["coverage"]

        self.assertEqual(
            coverage["ros_rows_covering_all_fantasy_playoff_weeks"],
            len(bundle.strength_model.players),
        )

    def test_reports_optional_full_horizon_source_gap_without_hiding_quorum(self):
        _, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        evidence = tuple(
            row
            for row in raw_rows(ensembles)
            if not (
                row.canonical_player_id == "p2"
                and row.week == 2
                and row.provider == "yahoo"
            )
        )

        coverage = build_bundle_data_readiness(
            build_weekly_bundle(evidence)
        )["coverage"]

        self.assertEqual(
            coverage["full_horizon_provider_availability"]["yahoo"],
            {"available_players": 3, "unavailable_players": 1},
        )
        self.assertEqual(coverage["unavailable_full_horizon_provider_cells"], 1)
        self.assertEqual(coverage["available_full_horizon_ensemble_players"], 4)
        self.assertEqual(coverage["unavailable_full_horizon_ensemble_players"], 0)

    def test_reports_provider_status_freshness_and_cross_provider_disagreement(self):
        _, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        evidence = []
        for row in raw_rows(ensembles):
            if (
                isinstance(row, WeeklyProjection)
                and
                row.canonical_player_id == "p1"
                and row.week == 1
                and row.provider in {"fantasypros", "espn"}
            ):
                row = replace(
                    row,
                    provider_status_observations=(
                        ProviderStatusObservation(
                            (
                                "Questionable"
                                if row.provider == "fantasypros"
                                else "Out"
                            ),
                            row.captured_at,
                            ProviderStatusScope.WEEKLY,
                            row.week,
                        ),
                    ),
                )
            evidence.append(row)
        bundle = build_weekly_bundle(tuple(evidence))

        report = build_bundle_data_readiness(bundle)
        coverage = report["coverage"]["provider_status_observations"]
        snapshot = build_data_readiness_snapshot(bundle)

        self.assertEqual(coverage["observation_count"], 2)
        self.assertEqual(coverage["player_count"], 1)
        self.assertEqual(coverage["disagreement_scope_count"], 1)
        self.assertEqual(
            coverage["by_provider"],
            {"espn": 1, "fantasypros": 1},
        )
        self.assertEqual(
            coverage["interpretation"],
            "observation_only_not_appearance_probability",
        )
        self.assertEqual(snapshot.provider_status_observation_count, 2)
        self.assertEqual(snapshot.provider_status_disagreement_scope_count, 1)
        self.assertIsNotNone(snapshot.latest_provider_status_observed_at)
        self.assertIn("not calibrated appearance probabilities", (
            snapshot.availability_limitation
        ))

    def test_rejects_non_bundle_input(self):
        with self.assertRaisesRegex(ValueError, "EngineBundle"):
            build_bundle_data_readiness(object())


if __name__ == "__main__":
    unittest.main()
