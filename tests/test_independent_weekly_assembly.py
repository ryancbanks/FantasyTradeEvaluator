from dataclasses import replace
import unittest

from tests.test_weekly_assembly import (
    host_snapshot,
    nfl_schedule,
    projection_artifact,
    projection_artifact_with_outside_player,
)
from tests.test_engine_bundle import engine_bundle as configured_engine_bundle
from trade_snapshot._app_support import bundle_summary, workbook_sources
from trade_snapshot._bundle_evidence import model_analysis_as_of
from trade_snapshot._gm_model_evidence import (
    build_gm_model_evidence,
    model_comparability_reasons,
)
from trade_snapshot._trade_timing_evidence import timing_evidence
from trade_snapshot._trade_timing_market import (
    prepare_market_evidence,
    projection_lineage_summary,
)
from trade_snapshot.app_service import LocalSearchRequest, _workbook_context
from trade_snapshot.capture_schema import CaptureProvider, RankingHorizon
from trade_snapshot.data_readiness import (
    build_bundle_data_readiness,
    build_data_readiness_snapshot,
)
from trade_snapshot.engine_bundle import EngineBundle
from trade_snapshot.gm_insights import _empty_result
from trade_snapshot.independent_waiver_pool import IndependentWaiverPool
from trade_snapshot.independent_weekly_assembly import (
    assemble_independent_weekly_engine,
)
from trade_snapshot.search_runner import TradeSearchSettings
from trade_snapshot.trade_space import TradeConstraints


def projection_artifacts(*, broad):
    providers = (
        (
            CaptureProvider.ESPN,
            (RankingHorizon.WEEKLY, RankingHorizon.ROS),
        ),
        (
            CaptureProvider.YAHOO,
            (RankingHorizon.WEEKLY, RankingHorizon.ROS),
        ),
    )
    if broad:
        providers += (
            (CaptureProvider.CBS, (RankingHorizon.ROS,)),
            (
                CaptureProvider.FFTODAY,
                (RankingHorizon.WEEKLY, RankingHorizon.ROS),
            ),
            (
                CaptureProvider.FANTASYSHARKS,
                (RankingHorizon.WEEKLY, RankingHorizon.ROS),
            ),
        )
    return tuple(
        projection_artifact(provider, horizon, week)
        for provider, horizons in providers
        for horizon in horizons
        for week in ((1, 2) if horizon is RankingHorizon.WEEKLY else (1,))
    )


def independent_bundle():
    return assemble_independent_weekly_engine(
        host_snapshot=host_snapshot(),
        projection_artifacts=projection_artifacts(broad=False),
        nfl_schedule=nfl_schedule(),
        scoring="PPR",
        expected_team_count=2,
    ).bundle


class IndependentWeeklyAssemblyTests(unittest.TestCase):
    def test_retains_projected_players_outside_the_bounded_waiver_pool(self):
        artifacts = tuple(
            projection_artifact_with_outside_player(provider, horizon, week)
            for provider in (CaptureProvider.ESPN, CaptureProvider.YAHOO)
            for horizon, weeks in (
                (RankingHorizon.WEEKLY, (1, 2)),
                (RankingHorizon.ROS, (1,)),
            )
            for week in weeks
        )

        assembled = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=artifacts,
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
        )

        self.assertEqual(assembled.player_lab_projections.player_ids, ("espn:204",))
        self.assertNotIn(
            "espn:204",
            {row.canonical_player_id for row in assembled.bundle.projections},
        )

    def test_builds_round_trippable_broad_engine_without_fantasypros(self):
        assembled = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=projection_artifacts(broad=True),
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=True,
        )
        bundle = assembled.bundle

        self.assertEqual(bundle.methodology_mode, "independent")
        self.assertEqual(bundle.ecr_snapshots, ())
        self.assertIsInstance(bundle.waiver_pool, IndependentWaiverPool)
        self.assertEqual(
            bundle.independent_power_disclosure.provider_names,
            ("cbs", "espn", "fantasysharks", "fftoday", "yahoo"),
        )
        observation_providers = {
            observation.provider
            for row in bundle.projections
            for observation in row.provider_observations
        }
        self.assertEqual(
            observation_providers,
            {"espn", "yahoo", "cbs", "fftoday", "fantasysharks"},
        )
        self.assertNotIn(
            "fantasypros",
            {row.provider for row in bundle.projection_evidence},
        )
        self.assertEqual(EngineBundle.from_record(bundle.to_record()), bundle)

    def test_core_fallback_uses_espn_and_yahoo(self):
        bundle = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=projection_artifacts(broad=False),
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=False,
        ).bundle

        self.assertEqual(
            bundle.independent_power_disclosure.provider_names, ("espn", "yahoo")
        )
        self.assertEqual(
            {
                observation.provider
                for row in bundle.projections
                for observation in row.provider_observations
            },
            {"espn", "yahoo"},
        )

    def test_reports_independent_readiness_without_fantasypros_provenance(self):
        bundle = independent_bundle()

        report = build_bundle_data_readiness(bundle)

        self.assertEqual(report["status"], "ready_with_known_limitations")
        self.assertEqual(
            report["capabilities"]["fantasypros_style_power"]["status"],
            "independent",
        )
        self.assertEqual(
            report["capabilities"]["player_lab"]["status"],
            "ready_with_limitations",
        )
        self.assertEqual(
            report["capabilities"]["fantasypros_comparison_benchmark"]["status"],
            "not_applicable",
        )
        self.assertEqual(
            report["bound_inputs"]["independent_power_disclosure"],
            {
                "captured_at": "2026-09-01T01:00:00.000000Z",
                "disclosure_id": bundle.independent_power_disclosure.disclosure_id,
                "policy_id": bundle.independent_power_disclosure.policy_id,
                "provider_names": ["espn", "yahoo"],
            },
        )
        self.assertEqual(
            report["bound_inputs"]["fantasypros_comparison_benchmark"],
            {
                "benchmark_id": None,
                "source_artifact_id": None,
                "captured_at": None,
            },
        )
        self.assertIsNone(
            report["bound_inputs"]["projection_source_manifest"]["manifest_id"]
        )
        self.assertEqual(
            report["coverage"]["projection_sources"]["evidence_basis"],
            "retained_normalized_projection_evidence",
        )
        self.assertEqual(
            report["coverage"]["projection_sources"]["source_count"],
            0,
        )
        self.assertEqual(
            report["coverage"]["fantasypros_comparison_team_count"],
            0,
        )
        self.assertFalse(
            report["capabilities"]["expected_standings"]["evidence"][
                "nfl_schedule_artifact_retained"
            ]
        )
        self.assertTrue(
            report["capabilities"]["expected_standings"]["evidence"][
                "projection_schedule_binding_complete"
            ]
        )
        self.assertIsNone(
            report["bound_inputs"]["league_binding"][
                "fantasypros_league_artifact_id"
            ]
        )
        self.assertEqual(report["bound_inputs"]["ecr_snapshot_ids"], {})

        snapshot = build_data_readiness_snapshot(bundle)
        self.assertEqual(snapshot.power_score_status, "independent")
        self.assertEqual(snapshot.fantasypros_comparison_team_count, 0)
        self.assertEqual(snapshot.projection_source_count, 0)
        self.assertEqual(snapshot.projection_source_provider_attempts, ())

    def test_independent_summary_and_workbook_provenance_use_retained_inputs(self):
        bundle = independent_bundle()

        summary = bundle_summary(bundle)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["league_key"], bundle.state.snapshot_id[:12])
        self.assertEqual(
            summary["league_label"],
            f"Independent league snapshot {bundle.state.snapshot_id[:12]}",
        )

        sources = workbook_sources(bundle)
        self.assertEqual(
            {row.name for row in sources},
            {
                "Independent weekly engine bundle",
                "Independent power policy disclosure",
                "espn projections (forecast vote)",
                "yahoo projections (forecast vote)",
            },
        )
        self.assertFalse(any("FantasyPros" in row.name for row in sources))

        team_ids = tuple(row.team_id for row in bundle.state.teams)
        context = _workbook_context(
            bundle,
            "independent-scenario-run",
            LocalSearchRequest(
                bundle.bundle_id,
                team_ids[0],
                (team_ids[1],),
                TradeConstraints(),
                TradeSearchSettings(),
                100,
                7,
            ),
        )
        self.assertEqual(
            context.nfl_schedule_id,
            "not-retained:independent-nfl-schedule-artifact",
        )
        self.assertEqual(
            context.ensemble_config_id,
            "not-retained:independent-ensemble-config-artifact",
        )
        self.assertEqual(
            context.formula_source_fit_id,
            "not-applicable:independent-power-policy-has-no-source-fit",
        )

    def test_builds_truthful_independent_gm_model_evidence(self):
        bundle = independent_bundle()

        evidence = build_gm_model_evidence(
            bundle,
            outgoing_count=1,
            incoming_count=1,
        )

        self.assertEqual(evidence.methodology_mode, "independent")
        self.assertEqual(evidence.methodology_status, "independent")
        self.assertEqual(
            evidence.formula_id,
            bundle.independent_power_disclosure.policy_id,
        )
        self.assertIsNone(evidence.formula_source_fit_id)
        self.assertIsNone(evidence.projection_source_manifest_id)
        self.assertIsNone(evidence.ensemble_config_id)
        self.assertEqual(evidence.ecr_ids, ())
        self.assertEqual(evidence.source_providers, ("espn", "yahoo"))
        self.assertIn(
            "power_shape_not_blind_holdout_validated_at_both_times",
            model_comparability_reasons(evidence, evidence),
        )

    def test_independent_gm_and_timing_reports_use_retained_model_evidence(self):
        bundle = independent_bundle()
        compatibility = {
            "scope": {},
            "power_methodology_status": "independent",
            "teams": [
                {"team_id": team.team_id}
                for team in bundle.state.teams
            ],
        }

        gm_report = _empty_result(bundle, compatibility)
        timing = timing_evidence(bundle, None)

        self.assertEqual(
            gm_report["analysis_as_of"], "2026-09-01T01:00:00Z"
        )
        for evidence in (gm_report["evidence"], timing):
            self.assertIsNone(evidence["league_binding_id"])
            self.assertEqual(
                evidence["strength_formula_id"],
                bundle.independent_power_disclosure.policy_id,
            )
            self.assertEqual(
                evidence["methodology_evidence_id"],
                bundle.independent_power_disclosure.disclosure_id,
            )
            self.assertIsNone(evidence["projection_source_manifest_id"])
            self.assertIsNone(evidence["ensemble_config_id"])
            self.assertEqual(evidence["ecr_ids"], [])
        self.assertEqual(
            timing["analysis_as_of"], "2026-09-01T01:00:00Z"
        )

    def test_independent_trade_timing_lineage_does_not_invent_artifacts(self):
        bundle = independent_bundle()

        summary = projection_lineage_summary(bundle)
        evidence_index = prepare_market_evidence(bundle)
        projection = bundle.projections[0]
        provider = evidence_index.provider_record(
            projection,
            projection.provider_observations[0],
        )

        self.assertIsNone(summary["projection_source_manifest_id"])
        self.assertIsNone(summary["ensemble_config_id"])
        self.assertEqual(summary["provider_names"], ["espn", "yahoo"])
        self.assertEqual(summary["source_artifact_count"], 0)
        self.assertEqual(summary["capture_attempt_count"], 0)
        self.assertEqual(summary["capture_status_counts"], {})
        self.assertEqual(summary["horizons"], ["rest_of_season", "weekly"])
        self.assertEqual(
            summary["normalized_evidence_row_count"],
            len(bundle.projection_evidence),
        )
        self.assertIsNone(provider["source_binding"])
        self.assertEqual(provider["provider"], "espn")
        self.assertEqual(provider["captured_at"], "2026-09-01T01:00:00Z")

    def test_configured_report_analysis_boundary_remains_unchanged(self):
        bundle = configured_engine_bundle()
        expected = max(
            bundle.source_manifest.host_captured_at,
            bundle.source_manifest.fantasypros_captured_at,
            *(row.captured_at for row in bundle.ecr_snapshots),
            *(
                row.captured_at
                for row in bundle.projection_source_manifest.sources
            ),
        )

        self.assertEqual(model_analysis_as_of(bundle), expected)

    def test_missing_independent_projection_schedule_binding_gates_simulation(self):
        bundle = independent_bundle()
        roster_player = bundle.rosters[0].player_ids[0]
        projections = tuple(
            replace(
                row,
                nfl_team_id=None,
                nfl_game_id=None,
                opponent_team_id=None,
                is_home=None,
            )
            if row.canonical_player_id == roster_player
            else row
            for row in bundle.projections
        )

        report = build_bundle_data_readiness(
            replace(bundle, projections=projections)
        )

        self.assertEqual(
            report["capabilities"]["fantasypros_style_power"]["status"],
            "independent",
        )
        self.assertEqual(
            report["capabilities"]["expected_standings"]["status"],
            "not_ready",
        )
        self.assertFalse(
            report["capabilities"]["expected_standings"]["evidence"][
                "projection_schedule_binding_complete"
            ]
        )

    def test_accepts_ros_only_fftoday_evidence_for_idp_safe_fallback(self):
        artifacts = tuple(
            row
            for row in projection_artifacts(broad=True)
            if not (
                row.provider is CaptureProvider.FFTODAY
                and row.horizon is RankingHorizon.WEEKLY
            )
        )

        bundle = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=artifacts,
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=True,
        ).bundle

        self.assertIn(
            "fftoday",
            bundle.independent_power_disclosure.provider_names,
        )

    def test_core_ensemble_requires_yahoo_projection_evidence(self):
        artifacts = tuple(
            row
            for row in projection_artifacts(broad=False)
            if row.provider is not CaptureProvider.YAHOO
        )

        with self.assertRaisesRegex(ValueError, "ESPN and Yahoo"):
            assemble_independent_weekly_engine(
                host_snapshot=host_snapshot(),
                projection_artifacts=artifacts,
                nfl_schedule=nfl_schedule(),
                scoring="PPR",
                expected_team_count=2,
                broad_consensus=False,
            )


if __name__ == "__main__":
    unittest.main()
