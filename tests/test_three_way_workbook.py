import unittest

from trade_snapshot._scenario_random import content_id
from trade_snapshot.three_way_search_records import (
    ThreeWayQualifiedResult,
    ThreeWaySearchRunDefinition,
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


def provenance_inputs(*, constraint_value=1):
    constraints = {"max_imbalance": constraint_value, "balanced_only": False}
    settings = {"minimum_displayed_power_delta": -5.0}
    request = {
        "allow_surrogate_power": False,
        "bundle_id": "engine_" + "a" * 64,
        "counterparty_team_ids": ["B", "C"],
        "primary_team_id": "A",
        "scenario_count": 100,
        "seed": 19,
        "settings": settings,
        "trade_constraints": constraints,
        "trade_format": "three_team",
    }
    run = ThreeWaySearchRunDefinition(
        "snapshot-1",
        "strength-1",
        ("A", "B", "C"),
        {
            "algorithm": "test",
            "candidate_order": {},
            "roster_adjustment_id": None,
            "scenario_run_id": "scenario-1",
            "settings": settings,
            "trade_constraints": constraints,
        },
        1 << 70,
    )
    return request, run


class ThreeWayWorkbookTests(unittest.TestCase):
    def test_export_provenance_is_canonical_complete_and_strict(self):
        request, run = provenance_inputs()
        provenance = ThreeWayExportProvenance.from_records(
            bundle_id=request["bundle_id"],
            waiver_pool_id="waiver-pool-1",
            request_id=content_id("app-search", request),
            request_record=request,
            search_run_definition=run,
            participant_team_names=("Alpha", "Bravo", "Charlie"),
            completed_candidate_count=run.total_candidate_count,
            free_agent_allocation_policy="Ascending team-ID order.",
        )

        self.assertEqual(
            provenance.trade_constraints_json,
            '{"balanced_only":false,"max_imbalance":1}',
        )
        self.assertIn("minimum_displayed_power_delta", provenance.power_settings_display)
        self.assertEqual(provenance.total_candidate_count, 1 << 70)
        self.assertEqual(provenance.request_record, request)
        self.assertEqual(provenance.search_run_definition, run)
        with self.assertRaisesRegex(ValueError, "completed search run"):
            ThreeWayExportProvenance.from_records(
                bundle_id=request["bundle_id"],
                waiver_pool_id="waiver-pool-1",
                request_id=content_id("app-search", request),
                request_record=request,
                search_run_definition=run,
                participant_team_names=("Alpha", "Bravo", "Charlie"),
                completed_candidate_count=run.total_candidate_count - 1,
            )
        changed_request = dict(request, seed=20)
        with self.assertRaisesRegex(ValueError, "request identity"):
            ThreeWayExportProvenance.from_records(
                bundle_id=request["bundle_id"],
                waiver_pool_id="waiver-pool-1",
                request_id=content_id("app-search", request),
                request_record=changed_request,
                search_run_definition=run,
                participant_team_names=("Alpha", "Bravo", "Charlie"),
                completed_candidate_count=run.total_candidate_count,
            )
        with self.assertRaisesRegex(ValueError, "exactly three"):
            ThreeWayExportProvenance.from_records(
                bundle_id=request["bundle_id"],
                waiver_pool_id="waiver-pool-1",
                request_id=content_id("app-search", request),
                request_record=request,
                search_run_definition=run,
                participant_team_names=("Alpha", "Bravo"),
                completed_candidate_count=run.total_candidate_count,
            )
        invalid_request, invalid_run = provenance_inputs()
        invalid_request["trade_constraints"] = {"bad": float("nan")}
        with self.assertRaisesRegex(ValueError, "finite JSON|JSON"):
            ThreeWayExportProvenance.from_records(
                bundle_id=invalid_request["bundle_id"],
                waiver_pool_id="waiver-pool-1",
                request_id="request-1",
                request_record=invalid_request,
                search_run_definition=invalid_run,
                participant_team_names=("Alpha", "Bravo", "Charlie"),
                completed_candidate_count=invalid_run.total_candidate_count,
            )

    def test_resolves_every_leg_and_team_impact_and_ranks_all_team_gains_first(self):
        losing = result(1, third_after=35)
        winning = result(2, third_after=45)
        rows = three_way_workbook_rows(
            (losing, winning),
            {"A": "Alpha", "B": "Beta", "C": "Gamma"},
            {"a": "A Player", "b": "B Player", "c": "C Player"},
            "holdout_validated",
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
                "holdout_validated",
            )

    def test_independent_power_keeps_its_truthful_methodology_status(self):
        row = three_way_workbook_rows(
            (result(),),
            {"A": "Alpha", "B": "Beta", "C": "Gamma"},
            {"a": "A Player", "b": "B Player", "c": "C Player"},
            "independent",
        )[0]

        self.assertEqual(row.power_methodology_status, "independent")


if __name__ == "__main__":
    unittest.main()
