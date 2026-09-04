import csv
import gzip
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trade_snapshot.draft_availability import AvailabilityStatus
from trade_snapshot.draft_roster_sources import load_roster_availability


class DraftRosterSourcesTests(unittest.TestCase):
    def _load(self, rows, *, weeks=range(1, 8), compressed=False, detailed=True):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ("roster.csv.gz" if compressed else "roster.csv")
            opener = gzip.open if compressed else open
            fields = ["season", "week", "game_type", "gsis_id", "status"]
            if detailed:
                fields.append("status_description_abbr")
            with opener(path, "wt", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(fields)
                writer.writerows(rows)
            return load_roster_availability(path, 2025, ("p",), weeks)

    def test_lags_evidence_compacts_stable_states_and_preserves_each_out_week(self):
        reports, coverage = self._load([
            [2025, 1, "REG", "p", "ACT", "A01"],
            [2025, 2, "REG", "p", "RES", "R01"],
            [2025, 3, "REG", "p", "RES", "R48"],
            [2025, 4, "REG", "p", "ACT", "A01"],
            [2025, 5, "REG", "p", "RES", ""],
            [2025, 6, "REG", "p", "RES", ""],
        ])
        self.assertEqual(
            [(row.week, row.source_week, row.status.value) for row in reports],
            [(2, 1, "active"), (3, 2, "ir"), (5, 4, "active"),
             (6, 5, "out"), (7, 6, "out")],
        )
        self.assertEqual(coverage["exact_ir_player_weeks"], 2)
        self.assertEqual(coverage["reserve_proxy_player_weeks"], 2)
        self.assertEqual(coverage["missing_player_weeks"], 0)
        self.assertIn("observed week 2", reports[1].source)
        self.assertIn("weekly_rosters/roster_weekly_2025.csv", reports[1].source)
        self.assertNotIn(AvailabilityStatus.SEASON_ENDING_IR, {row.status for row in reports})

    def test_reconstructed_2015_status_is_not_read_or_imported(self):
        reports, coverage = load_roster_availability(
            "does-not-exist.csv", 2015, ("p",), (1, 2, 3)
        )
        self.assertEqual(reports, ())
        self.assertEqual(coverage["method"], "unavailable_reconstructed_season_status")
        self.assertEqual(coverage["missing_player_weeks"], 2)
        self.assertIn("season-level", coverage["limitations"][-1])

    def test_conflicting_duplicate_and_status_labels_remain_unknown(self):
        reports, coverage = self._load([
            [2025, 1, "REG", "p", "ACT", "A01"],
            [2025, 1, "REG", "p", "RES", "R01"],
            [2025, 2, "REG", "p", "RES", "A01"],
            [2025, 3, "REG", "p", "ACT", "R01"],
            [2025, 4, "REG", "p", "RES", "R48"],
            [2025, 4, "REG", "p", "RES", "R48"],
            [2025, 5, "REG", "p", "CUT", "R01"],
        ])
        self.assertEqual([(row.week, row.status.value) for row in reports], [(5, "ir")])
        self.assertEqual(coverage["ambiguous_player_weeks"], 4)
        self.assertEqual(coverage["duplicate_rows"], 2)

    def test_unknown_detail_cannot_confirm_active_or_guess_ir(self):
        reports, coverage = self._load([
            [2025, 1, "REG", "p", "ACT", "I01"],
            [2025, 2, "REG", "p", "RES", "R49"],
            [2025, 3, "REG", "p", "UNKNOWN", "A01"],
            [2025, 4, "REG", "p", "", "R01"],
        ])
        self.assertEqual(
            [(row.week, row.status.value) for row in reports], [(3, "out"), (5, "ir")]
        )
        self.assertEqual(coverage["unknown_status_player_weeks"], 2)
        self.assertEqual(coverage["reserve_proxy_player_weeks"], 1)

    def test_only_prior_regular_season_selected_player_rows_are_usable(self):
        reports, coverage = self._load([
            [2025, "", "REG", "p", "ACT", "A01"],
            [2025, 0, "REG", "p", "ACT", "A01"],
            [2025, 1, "PRE", "p", "RES", "R01"],
            [2024, 1, "REG", "p", "RES", "R01"],
            [2025, 1, "REG", "other", "RES", "R01"],
            [2025, 3, "REG", "p", "RES", "R01"],
        ], weeks=(1, 2, 3))
        self.assertEqual(reports, ())
        self.assertEqual(coverage["unusable_week_rows"], 2)
        self.assertEqual(coverage["missing_player_weeks"], 2)

    def test_gzip_and_missing_optional_detail_use_generic_status_only(self):
        reports, coverage = self._load([
            [2025, 1, "REG", "p", "ACT"],
            [2025, 2, "REG", "p", "INA"],
        ], compressed=True, detailed=False)
        self.assertEqual(
            [(row.week, row.status.value) for row in reports], [(2, "active"), (3, "out")]
        )
        self.assertEqual(coverage["exact_ir_player_weeks"], 0)
        self.assertTrue(coverage["source_url"].endswith(".csv.gz"))

    def test_boolean_week_cannot_hide_behind_integer_duplicate(self):
        with self.assertRaisesRegex(ValueError, "weeks must be integers"):
            load_roster_availability("unused.csv", 2025, ("p",), (1, True))

    def test_sparse_calendar_cannot_skip_intervening_ir_clearance(self):
        with self.assertRaisesRegex(ValueError, "complete calendar"):
            load_roster_availability("unused.csv", 2025, ("p",), (1, 3))


if __name__ == "__main__":
    unittest.main()
