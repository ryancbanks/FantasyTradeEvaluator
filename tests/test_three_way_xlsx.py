from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from trade_snapshot.three_way_workbook import (
    ThreeWayExportProvenance,
    ThreeWayWorkbookRow,
    ThreeWayWorkbookTeamImpact,
    ThreeWayWorkbookTransfer,
)
from trade_snapshot.three_way_xlsx import export_three_way_trade_workbook
from trade_snapshot.workbook_model import (
    TradeWorkbookContext,
    WorkbookSource,
    WorkbookTeamOutlook,
)


NOW = datetime(2026, 9, 2, 18, tzinfo=timezone.utc)


def context():
    return TradeWorkbookContext(
        snapshot_id="snapshot-3way",
        strength_model_id="model-3way",
        scenario_run_id="scenario-3way",
        primary_team_id="A",
        primary_team_name="Alpha",
        generated_at=NOW,
        minimum_power_delta=-5,
        scenario_count=10_000,
        power_engine_mode="exact",
        calibration_status="exact",
        methodology_evidence_kind="exact_attestation",
        methodology_record_id="attestation-3way",
        formula_id="formula-3way",
        formula_source_fit_id="fit-3way",
        methodology_fingerprint_id="fingerprint-3way",
        formula_action="reuse",
        methodology_current_evidence_id="verification-3way",
        methodology_quality_gate="exact_attestation_v1",
        methodology_holdout_count=100,
        holdout_max_absolute_score_error=1e-6,
        holdout_display_match_rate=1.0,
        exact_balanced_package_sizes=(1, 2, 3),
        sources=(
            WorkbookSource("FantasyPros ECR", "ecr-3way", NOW),
            WorkbookSource("ESPN projections", "espn-3way", NOW),
            WorkbookSource("Yahoo projections", "yahoo-3way", NOW),
        ),
    )


def provenance(*, adjustments=True):
    return ThreeWayExportProvenance.from_records(
        request_id="app-search-request-3way",
        search_run_id="three-way-search-run-v1-test",
        participant_team_ids=("A", "B", "C"),
        participant_team_names=("Alpha", "Bravo", "Charlie"),
        total_candidate_count=1 << 70,
        seed=-9_007_199_254_740_991,
        trade_constraint_record={
            "balanced_only": False,
            "incoming_filter": {
                "player_ids": ["C-received"],
                "player_mode": "include",
                "position_mode": None,
                "positions": [],
            },
            "max_imbalance": 1,
            "require_no_drops": not adjustments,
        },
        power_settings_record={
            "checkpoint_interval": 1000,
            "minimum_displayed_power_delta": -5.0,
        },
        free_agent_allocation_policy=(
            "Scarce replacements use ascending team-ID order."
            if adjustments
            else None
        ),
    )


def impact(team_id, name, sent, received, before, after):
    return ThreeWayWorkbookTeamImpact(
        team_id,
        name,
        (f"{team_id}-sent",),
        (sent,),
        (f"{team_id}-received",),
        (received,),
        (f"{team_id}-add",),
        (f"{name} Add",),
        (f"{team_id}-drop",),
        (f"{name} Drop",),
        1.25,
        before,
        after,
    )


def trade(*, all_gain=True, candidate_index=7):
    malicious = '=HYPERLINK("https://bad.example","click")'
    return ThreeWayWorkbookRow(
        candidate_index,
        (
            ThreeWayWorkbookTransfer(
                "A", "Alpha", "B", "Bravo", ("A-sent",), (malicious,)
            ),
            ThreeWayWorkbookTransfer(
                "B", "Bravo", "C", "Charlie", ("B-sent",), ("Bravo Sent",)
            ),
            ThreeWayWorkbookTransfer(
                "C", "Charlie", "A", "Alpha", ("C-sent",), ("Charlie Sent",)
            ),
        ),
        (
            impact("A", "Alpha", malicious, "Charlie Sent", 0.25, 0.30),
            impact("B", "Bravo", "Bravo Sent", malicious, 0.40, 0.45),
            impact(
                "C",
                "Charlie",
                "Charlie Sent",
                "Bravo Sent",
                0.50,
                0.55 if all_gain else 0.45,
            ),
        ),
        "extrapolated",
    )


def outlook():
    return (
        WorkbookTeamOutlook("A", "Alpha", 2, 1, 0, 8.2, 5.8, 0, 2.1, 0.72),
        WorkbookTeamOutlook("B", "Bravo", 1, 2, 0, 6.4, 7.6, 0, 5.4, 0.31),
        WorkbookTeamOutlook("C", "Charlie", 3, 0, 0, 9.0, 5.0, 0, 1.4, 0.84),
    )


class ThreeWayExcelExportTests(unittest.TestCase):
    def test_writes_formula_auditable_safe_workbook_with_required_sheets(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "three-way-results.xlsx"
            result = export_three_way_trade_workbook(
                target,
                context(),
                provenance(),
                (
                    trade(candidate_index=1_000_000_000_000_001),
                    trade(all_gain=False, candidate_index=(1 << 63) + 123),
                ),
                outlook(),
            )
            self.assertEqual(result, target.resolve())
            self.assertEqual(tuple(target.parent.glob(".*.tmp.xlsx")), ())
            with ZipFile(target) as archive:
                names = set(archive.namelist())
                workbook_xml = archive.read("xl/workbook.xml").decode()
                worksheets = "".join(
                    archive.read(name).decode()
                    for name in names
                    if name.startswith("xl/worksheets/sheet")
                    and name.endswith(".xml")
                )
                shared = archive.read("xl/sharedStrings.xml").decode()
                relationships = "".join(
                    archive.read(name).decode()
                    for name in names
                    if name.endswith(".rels")
                )

        for name in (
            "Best Three-Way",
            "All Qualified",
            "Team Outlook",
            "Run Details",
        ):
            self.assertIn(name, workbook_xml)
        for formula in (
            "I8-H8",
            "R8-Q8",
            "AA8-Z8",
            "AND(J8&gt;0,S8&gt;0,AB8&gt;0)",
            "J8+S8+AB8",
        ):
            self.assertIn(formula, worksheets)
        self.assertIn("Player Movement", shared)
        self.assertIn("All 3 Improve", shared)
        self.assertIn("Power Method Evidence", shared)
        self.assertIn("HYPERLINK", shared)
        self.assertIn("Alpha → Bravo", shared)
        self.assertIn("three-way", shared.casefold())
        self.assertIn("extrapolated", shared.casefold())
        self.assertIn("attestation-3way", shared)
        self.assertIn("app-search-request-3way", shared)
        self.assertIn("three-way-search-run-v1-test", shared)
        self.assertIn("Alpha (A)", shared)
        self.assertIn(str(1 << 70), shared)
        self.assertIn(str(-9_007_199_254_740_991), shared)
        self.assertIn("C-received", shared)
        self.assertIn("minimum_displayed_power_delta", shared)
        self.assertIn("ascending team-ID order", shared)
        self.assertIn("1000000000000001", shared)
        self.assertIn(str((1 << 63) + 123), shared)
        self.assertFalse(any(name.startswith("xl/externalLinks") for name in names))
        self.assertNotIn('TargetMode="External"', relationships)

    def test_overwrites_atomically_and_preserves_target_if_generation_fails(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "three-way-results.xlsx"
            export_three_way_trade_workbook(
                target, context(), provenance(), (trade(),), outlook()
            )
            first = target.read_bytes()
            export_three_way_trade_workbook(
                target, context(), provenance(adjustments=False), (), outlook()
            )
            second = target.read_bytes()
            self.assertNotEqual(first, second)

            def fail_after_writing(path, *_args):
                path.write_bytes(b"incomplete temporary workbook")
                raise RuntimeError("simulated generation failure")

            with patch(
                "trade_snapshot.three_way_xlsx._write_workbook",
                side_effect=fail_after_writing,
            ), self.assertRaisesRegex(RuntimeError, "simulated"):
                export_three_way_trade_workbook(
                    target, context(), provenance(), (trade(),), outlook()
                )

            self.assertEqual(target.read_bytes(), second)
            self.assertEqual(tuple(target.parent.glob(".*.tmp.xlsx")), ())

    def test_surrogate_provenance_stays_explicitly_extrapolated(self):
        surrogate = replace(
            context(),
            power_engine_mode="surrogate",
            calibration_status="surrogate",
            methodology_evidence_kind="surrogate_disclosure",
            methodology_record_id="surrogate-disclosure-3way",
            formula_action="recalibrate",
            methodology_quality_gate=(
                "converged_identifiable_training-exact_full-blind-design_v1"
            ),
            exact_balanced_package_sizes=(),
        )
        with TemporaryDirectory() as directory:
            target = Path(directory) / "surrogate-three-way.xlsx"
            export_three_way_trade_workbook(
                target,
                surrogate,
                provenance(),
                (replace(trade(), power_methodology_status="surrogate_extrapolated"),),
                outlook(),
            )
            with ZipFile(target) as archive:
                shared = archive.read("xl/sharedStrings.xml").decode()

        self.assertIn("SURROGATE / APPROXIMATE", shared)
        self.assertIn("surrogate_extrapolated", shared)
        self.assertIn("three-way", shared.casefold())
        self.assertIn("outside", shared.casefold())

    def test_validates_suffix_row_types_and_excel_limit(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "end in .xlsx"):
                export_three_way_trade_workbook(
                    Path(directory) / "results.csv",
                    context(),
                    provenance(),
                    (),
                    outlook(),
                )
            with self.assertRaisesRegex(ValueError, "ThreeWayWorkbookRow"):
                export_three_way_trade_workbook(
                    Path(directory) / "results.xlsx",
                    context(),
                    provenance(),
                    (object(),),
                    outlook(),
                )
            with patch("trade_snapshot.three_way_xlsx.MAX_THREE_WAY_EXPORT_ROWS", 1):
                with self.assertRaisesRegex(ValueError, "at most 1"):
                    export_three_way_trade_workbook(
                        Path(directory) / "results.xlsx",
                        context(),
                        provenance(),
                        (trade(), trade(candidate_index=8)),
                        outlook(),
                    )


if __name__ == "__main__":
    unittest.main()
