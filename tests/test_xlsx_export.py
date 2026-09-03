from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from trade_snapshot.data_readiness import DataReadinessSnapshot
from trade_snapshot.workbook_model import (
    TradeWorkbookContext,
    WorkbookSource,
    WorkbookTeamOutlook,
    WorkbookTradeRow,
)
from trade_snapshot.xlsx_export import export_trade_workbook


NOW = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)


def readiness():
    return DataReadinessSnapshot(
        provider_cell_count=120,
        direct_provider_cells=70,
        ros_derived_provider_cells=30,
        schedule_derived_availability_cells=10,
        unavailable_provider_cells=8,
        unattributed_provider_cells=2,
        first_week_scheduled_games=8,
        first_week_games_missing_kickoff=2,
        source_capture_timestamp_count=5,
        earliest_source_capture_at=NOW,
        latest_source_capture_at=NOW,
        scenario_player_score_floor=None,
        fantasypros_comparison_team_count=2,
        fantasypros_comparison_policy=(
            "Comparison only; never blended into local playoff odds."
        ),
        projection_source_count=3,
        captured_projection_source_attempts=3,
        not_published_projection_source_attempts=1,
        unavailable_projection_source_attempts=1,
        provider_total_projection_sources=3,
        locally_recomputed_projection_sources=0,
        base_format_only_projection_sources=3,
        exact_host_rules_projection_sources=0,
        projection_source_scoring_formats=("PPR",),
        projection_source_provider_attempts=(
            ("espn", 1, 0, 0),
            ("fantasypros", 1, 0, 0),
            ("yahoo", 1, 1, 1),
        ),
        provider_status_observation_count=0,
        provider_status_disagreement_scope_count=0,
        latest_provider_status_observed_at=None,
        power_score_status="ready_with_holdout_validated_scope",
        trade_search_status="ready_with_limitations",
        expected_standings_status="ready_with_limitations",
        playoff_model_status="model_estimate_with_limitations",
        custom_scoring_limitation="Custom scoring has not been fully recomputed.",
        availability_limitation="Future player availability is not typed.",
        correlation_limitation="Shared outcome correlations are not calibrated.",
        marginal_uncertainty_limitation=(
            "Marginal player uncertainty is not calibrated."
        ),
        championship_proxy_limitation="Championship odds are a playoff-field proxy.",
        host_settlement_policy_limitation=(
            "Host tiebreak settlement is partially inferred."
        ),
        as_of_time_limitation="Two first-week games lack kickoff timestamps.",
        ros_allocation_limitation="ROS residuals use even weekly allocation.",
    )


def context():
    return TradeWorkbookContext(
        snapshot_id="snapshot-1",
        scoring_profile_id="scoring-profile-1",
        nfl_schedule_id="nfl-schedule-1",
        ensemble_config_id="ensemble-config-1",
        strength_model_id="model-1",
        scenario_run_id="scenario-1",
        primary_team_id="primary",
        primary_team_name="Primary Team",
        generated_at=NOW,
        minimum_power_delta=-5,
        scenario_count=10_000,
        power_engine_mode="holdout_validated",
        calibration_status="exact",
        methodology_evidence_kind="blind_holdout_attestation",
        methodology_record_id="attestation-1",
        formula_id="formula-1",
        formula_source_fit_id="fit-1",
        methodology_fingerprint_id="fingerprint-1",
        formula_action="reuse",
        methodology_current_evidence_id="verification-1",
        methodology_quality_gate="blind_holdout_validation_v1",
        methodology_holdout_count=100,
        holdout_max_absolute_score_error=1e-6,
        holdout_display_match_rate=1.0,
        holdout_validated_balanced_package_sizes=(1, 2, 3, 4),
        data_readiness=readiness(),
        sources=(
            WorkbookSource("FantasyPros ECR", "ecr-1", NOW),
            WorkbookSource("ESPN projections", "espn-1", NOW),
            WorkbookSource("Yahoo projections", "yahoo-1", NOW),
        ),
    )


def trade(*, mutual=True, candidate_index=0):
    return WorkbookTradeRow(
        counterparty_team_id="other",
        counterparty_team_name="Other Team",
        outgoing_player_ids=("p1",),
        outgoing_player_names=("=HYPERLINK(\"https://bad.example\",\"click\")",),
        incoming_player_ids=("p2", "p3"),
        incoming_player_names=("Player Two", "Player Three"),
        primary_power_delta=1.2,
        counterparty_power_delta=0.5,
        primary_playoff_before=0.25,
        primary_playoff_after=0.30,
        counterparty_playoff_before=0.40,
        counterparty_playoff_after=0.45 if mutual else 0.35,
        candidate_index=candidate_index,
        power_methodology_status=(
            "holdout_validated" if mutual else "extrapolated"
        ),
    )


def surrogate_context():
    return replace(
        context(),
        power_engine_mode="surrogate",
        calibration_status="surrogate",
        methodology_evidence_kind="surrogate_disclosure",
        methodology_record_id="surrogate-disclosure-1",
        formula_action="recalibrate",
        methodology_quality_gate=(
            "converged_identifiable_training-exact_full-blind-design_v1"
        ),
        holdout_max_absolute_score_error=0.25,
        holdout_display_match_rate=0.91,
        holdout_validated_balanced_package_sizes=(),
    )


def outlook():
    return (
        WorkbookTeamOutlook(
            "primary", "Primary Team", 2, 1, 0, 8.2, 5.8, 0, 2.1, 0.72,
            current_rank=2,
            expected_final_points_for=1400.5,
            expected_final_points_against=1325.25,
            rank_distribution=(0.2, 0.8),
            seed_distribution=(0.3, 0.42),
        ),
        WorkbookTeamOutlook("other", "Other Team", 1, 2, 0, 6.4, 7.6, 0, 5.4, 0.31),
    )


class ExcelExportTests(unittest.TestCase):
    def test_writes_atomic_formula_auditable_workbook_without_formula_injection(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "trade-results.xlsx"
            result = export_trade_workbook(
                target, context(), (trade(), trade(mutual=False, candidate_index=1)), outlook()
            )
            self.assertEqual(result, target.resolve())
            self.assertTrue(target.is_file())
            self.assertEqual(tuple(target.parent.glob(".*.tmp.xlsx")), ())
            with ZipFile(target) as archive:
                names = set(archive.namelist())
                workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
                combined = "".join(
                    archive.read(name).decode("utf-8")
                    for name in names
                    if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                )
                shared = archive.read("xl/sharedStrings.xml").decode("utf-8")

        for sheet_name in ("Best Trades", "All Qualified", "Team Outlook", "Run Details"):
            self.assertIn(sheet_name, workbook_xml)
        self.assertIn("N8-M8", combined)
        self.assertIn("AND(O8&gt;0,R8&gt;0)", combined)
        self.assertIn("HYPERLINK", shared)
        self.assertIn("Power Method Evidence", shared)
        self.assertIn("Blind-Validated FantasyPros-Power Scope", shared)
        self.assertIn("Host-Settlement-Policy Limitation", shared)
        self.assertIn("Expected PF", shared)
        self.assertIn("Rank Probabilities", shared)
        self.assertIn("1: 20.0%; 2: 80.0%", shared)
        self.assertIn("Direct Provider Projection Cells", shared)
        self.assertIn("ROS-Derived Provider Projection Cells", shared)
        self.assertIn("Power-Score Readiness", shared)
        self.assertIn("Expected-Standings Readiness", shared)
        self.assertIn("Playoff Model Readiness", shared)
        self.assertIn("model_estimate_with_limitations", shared)
        self.assertIn("Projection Source Artifacts", shared)
        self.assertIn("Provider-Total Projection Sources", shared)
        self.assertIn("Projection Attempts (yahoo)", shared)
        self.assertIn("captured=1; not_published=1; unavailable=1", shared)
        self.assertIn("Provider Status Observations", shared)
        self.assertIn("Provider Status Disagreement Scopes", shared)
        self.assertIn("NONE RETAINED", shared)
        self.assertIn("Custom-Scoring Limitation", shared)
        self.assertIn("Source Capture Window (Seconds)", shared)
        self.assertIn("FantasyPros Comparison Team Coverage", shared)
        self.assertIn("Scenario Player-Score Floor", shared)
        self.assertIn("UNBOUNDED", shared)
        self.assertIn("never blended into local playoff odds", shared)
        self.assertIn("Marginal-Uncertainty Limitation", shared)
        self.assertIn("Future player availability is not typed.", shared)
        self.assertIn("Shared outcome correlations are not calibrated.", shared)
        self.assertIn("Championship odds are a playoff-field proxy.", shared)
        self.assertIn("As-of-Time Limitation", shared)
        self.assertIn("ROS Weekly-Allocation Limitation", shared)
        self.assertIn("ROS residuals use even weekly allocation.", shared)
        self.assertIn("attestation-1", shared)
        self.assertIn("scoring-profile-1", shared)
        self.assertIn("nfl-schedule-1", shared)
        self.assertIn("ensemble-config-1", shared)
        self.assertIn("exact", shared)
        self.assertIn("extrapolated", shared)
        self.assertFalse(any(name.startswith("xl/externalLinks") for name in names))

    def test_overwrites_instead_of_appending_duplicate_rows(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "trade-results.xlsx"
            export_trade_workbook(target, context(), (trade(),), outlook())
            first_size = target.stat().st_size
            export_trade_workbook(target, context(), (), outlook())
            second_size = target.stat().st_size

        self.assertNotEqual(first_size, second_size)

    def test_surrogate_workbook_has_no_exact_scope_and_repeats_the_warning(self):
        row = replace(trade(), power_methodology_status="surrogate")
        extrapolated = replace(
            trade(candidate_index=1),
            power_methodology_status="surrogate_extrapolated",
        )
        with TemporaryDirectory() as directory:
            target = Path(directory) / "surrogate-results.xlsx"
            export_trade_workbook(
                target, surrogate_context(), (row, extrapolated), outlook()
            )
            with ZipFile(target) as archive:
                shared = archive.read("xl/sharedStrings.xml").decode("utf-8")

        self.assertIn("SURROGATE / APPROXIMATE", shared)
        self.assertIn("NONE — this engine is a SURROGATE approximation", shared)
        self.assertIn("did not reproduce FantasyPros exactly", shared)
        self.assertIn("surrogate", shared)
        self.assertIn("surrogate_extrapolated", shared)

    def test_requires_xlsx_suffix_and_valid_probability(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "end in .xlsx"):
                export_trade_workbook(Path(directory) / "results.csv", context(), (), outlook())
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            WorkbookTeamOutlook("t", "Team", 0, 0, 0, 1, 1, 0, 1, 1.1)

    def test_seed_distribution_reconciles_to_playoff_probability(self):
        with self.assertRaisesRegex(ValueError, "wrong probability total"):
            WorkbookTeamOutlook(
                "t", "Team", 0, 0, 0, 1, 1, 0, 1, 0.9,
                seed_distribution=(0.1,),
            )


if __name__ == "__main__":
    unittest.main()
