from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tests.test_engine_bundle import engine_bundle
from tests.test_search_store import definition, qualified
from trade_snapshot.league_search import (
    LeagueSearchOutcome,
    LeagueSearchProgress,
    TeamPairSearchOutcome,
)
from trade_snapshot.search_runner import TradeSearchOutcome, TradeSearchProgress
from trade_snapshot.search_store import SearchStore
from trade_snapshot.workbook_model import (
    TradeWorkbookContext,
    WorkbookSource,
    WorkbookTeamOutlook,
    WorkbookTradeRow,
    WorkbookTradeRows,
    workbook_trade_rows,
)
from trade_snapshot.xlsx_export import export_trade_workbook


NOW = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)


def context():
    return TradeWorkbookContext(
        snapshot_id="snapshot-1",
        strength_model_id="model-1",
        scenario_run_id="scenario-1",
        primary_team_id="primary",
        primary_team_name="Primary Team",
        generated_at=NOW,
        minimum_power_delta=-5,
        scenario_count=10_000,
        power_engine_mode="exact",
        calibration_status="exact",
        methodology_evidence_kind="exact_attestation",
        methodology_record_id="attestation-1",
        formula_id="formula-1",
        formula_source_fit_id="fit-1",
        methodology_fingerprint_id="fingerprint-1",
        formula_action="reuse",
        methodology_current_evidence_id="verification-1",
        methodology_quality_gate="exact_attestation_v1",
        methodology_holdout_count=100,
        holdout_max_absolute_score_error=1e-6,
        holdout_display_match_rate=1.0,
        exact_balanced_package_sizes=(1, 2, 3, 4),
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
        power_methodology_status="exact" if mutual else "extrapolated",
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
        exact_balanced_package_sizes=(),
    )


def outlook():
    return (
        WorkbookTeamOutlook("primary", "Primary Team", 2, 1, 0, 8.2, 5.8, 0, 2.1, 0.72),
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
        self.assertIn("Exact FantasyPros-Power Scope", shared)
        self.assertIn("attestation-1", shared)
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

    def test_large_workbooks_stream_rows_and_keep_filters_without_tables(self):
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.xlsx_export._MAX_IN_MEMORY_TRADE_ROWS", 1
        ):
            target = Path(directory) / "streaming-results.xlsx"
            export_trade_workbook(
                target,
                context(),
                (trade(), trade(mutual=False, candidate_index=1)),
                outlook(),
            )
            with ZipFile(target) as archive:
                names = set(archive.namelist())
                worksheets = "".join(
                    archive.read(name).decode("utf-8")
                    for name in names
                    if name.startswith("xl/worksheets/sheet")
                    and name.endswith(".xml")
                )

        self.assertFalse(any(name.startswith("xl/tables/") for name in names))
        self.assertGreaterEqual(worksheets.count("<autoFilter"), 3)
        self.assertIn("MAX(T8:T9)", worksheets)

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

    def test_restartable_rows_stream_to_the_same_workbook_as_a_materialized_tuple(self):
        bundle = engine_bundle()
        results = (
            qualified(
                0,
                outgoing_player_ids=("p1",),
                incoming_player_ids=("q1",),
                primary_playoff_before=20,
                primary_playoff_after=25,
                counterparty_playoff_before=30,
                counterparty_playoff_after=35,
            ),
            qualified(
                1,
                outgoing_player_ids=("p2",),
                incoming_player_ids=("q2",),
                primary_playoff_before=20,
                primary_playoff_after=40,
                counterparty_playoff_before=30,
                counterparty_playoff_after=25,
            ),
        )
        with TemporaryDirectory() as directory:
            run = definition(total_candidate_count=2)
            database = Path(directory) / "results.sqlite3"
            with SearchStore(database, run) as store:
                store.upsert_qualified_results(results, next_candidate_index=2)
            pair_progress = TradeSearchProgress(run.run_id, 2, 2, 2, 2, 1)
            outcome = LeagueSearchOutcome(
                LeagueSearchProgress(1, 1, None, 2, 2, 2, 1),
                (
                    TeamPairSearchOutcome(
                        "other",
                        TradeSearchOutcome.from_database(pair_progress, database),
                    ),
                ),
            )
            rows = workbook_trade_rows(
                outcome,
                {team.team_id: team.name for team in bundle.state.teams},
                bundle.player_names,
                bundle.methodology_evidence,
            )
            self.assertIsInstance(rows, WorkbookTradeRows)
            materialized = tuple(rows)
            self.assertEqual(rows, materialized)
            streamed_path = Path(directory) / "streamed.xlsx"
            materialized_path = Path(directory) / "materialized.xlsx"
            with patch(
                "trade_snapshot.search_runner.read_search_results",
                side_effect=AssertionError("export must stream stored results"),
            ):
                export_trade_workbook(streamed_path, context(), rows, outlook())
            export_trade_workbook(
                materialized_path, context(), materialized, outlook()
            )
            with ZipFile(streamed_path) as streamed, ZipFile(
                materialized_path
            ) as eager:
                comparable_names = {
                    name
                    for name in streamed.namelist()
                    if name.startswith("xl/")
                }
                self.assertEqual(comparable_names, set(eager.namelist()).intersection(comparable_names))
                for name in comparable_names:
                    self.assertEqual(streamed.read(name), eager.read(name), name)


if __name__ == "__main__":
    unittest.main()
