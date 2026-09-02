import unittest

from trade_snapshot.three_way_search_records import (
    ThreeWayQualifiedResult,
    ThreeWayTeamResult,
)
from trade_snapshot.three_way_trade import TradeTransfer
from trade_snapshot.three_way_workbook import three_way_workbook_rows
from trade_snapshot.three_way_workbook import ThreeWayExportProvenance


TRANSFERS = (
    TradeTransfer("A", "B", ("a",)),
    TradeTransfer("B", "C", ("b",)),
    TradeTransfer("C", "A", ("c",)),
)


def result(index=0, *, third_after=35.0):
    return ThreeWayQualifiedResult(
        index,
        TRANSFERS,
        (
            ThreeWayTeamResult("A", ("a",), ("c",), (), (), 1, 1, 20, 25),
            ThreeWayTeamResult("B", ("b",), ("a",), (), (), 2, 2, 30, 32),
            ThreeWayTeamResult(
                "C", ("c",), ("b",), (), (), 3, 3, 40, third_after
            ),
        ),
    )


class ThreeWayWorkbookTests(unittest.TestCase):
    def test_export_provenance_is_canonical_complete_and_strict(self):
        provenance = ThreeWayExportProvenance.from_records(
            request_id="request-1",
            search_run_id="run-1",
            participant_team_ids=("A", "B", "C"),
            participant_team_names=("Alpha", "Bravo", "Charlie"),
            total_candidate_count=1 << 70,
            seed=19,
            trade_constraint_record={"max_imbalance": 1, "balanced_only": False},
            power_settings_record={"minimum_displayed_power_delta": -5.0},
            free_agent_allocation_policy="Ascending team-ID order.",
        )

        self.assertEqual(
            provenance.trade_constraints_json,
            '{"balanced_only":false,"max_imbalance":1}',
        )
        self.assertIn("minimum_displayed_power_delta", provenance.power_settings_display)
        self.assertEqual(provenance.total_candidate_count, 1 << 70)
        with self.assertRaisesRegex(ValueError, "exactly three"):
            ThreeWayExportProvenance.from_records(
                request_id="request-1",
                search_run_id="run-1",
                participant_team_ids=("A", "B"),
                participant_team_names=("Alpha", "Bravo"),
                total_candidate_count=1,
                seed=1,
                trade_constraint_record={},
                power_settings_record={},
            )
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            ThreeWayExportProvenance.from_records(
                request_id="request-1",
                search_run_id="run-1",
                participant_team_ids=("A", "B", "C"),
                participant_team_names=("Alpha", "Bravo", "Charlie"),
                total_candidate_count=1,
                seed=1,
                trade_constraint_record={"bad": float("nan")},
                power_settings_record={},
            )

    def test_resolves_every_leg_and_team_impact_and_ranks_all_team_gains_first(self):
        losing = result(1, third_after=35)
        winning = result(2, third_after=45)
        rows = three_way_workbook_rows(
            (losing, winning),
            {"A": "Alpha", "B": "Beta", "C": "Gamma"},
            {"a": "A Player", "b": "B Player", "c": "C Player"},
            "exact",
        )

        self.assertEqual(tuple(row.candidate_index for row in rows), (2, 1))
        self.assertTrue(rows[0].all_teams_gain)
        self.assertEqual(rows[0].power_methodology_status, "extrapolated")
        self.assertEqual(
            rows[0].transfers[0].description,
            "Alpha → Beta: A Player",
        )
        self.assertEqual(rows[0].team_impacts[0].sent_player_names, ("A Player",))
        self.assertAlmostEqual(rows[0].team_impacts[0].playoff_delta, 0.05)

    def test_surrogate_is_always_extrapolated_and_missing_names_fail_closed(self):
        row = three_way_workbook_rows(
            (result(),),
            {"A": "Alpha", "B": "Beta", "C": "Gamma"},
            {"a": "A Player", "b": "B Player", "c": "C Player"},
            "surrogate",
        )[0]
        self.assertEqual(row.power_methodology_status, "surrogate_extrapolated")
        with self.assertRaisesRegex(ValueError, "missing display name"):
            three_way_workbook_rows(
                (result(),),
                {"A": "Alpha", "B": "Beta", "C": "Gamma"},
                {"a": "A Player", "b": "B Player"},
                "exact",
            )


if __name__ == "__main__":
    unittest.main()
