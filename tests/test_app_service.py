from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from tests.test_engine_bundle import engine_bundle
from tests.test_surrogate_disclosure import surrogate_bundle
from trade_snapshot.app_service import LocalAppService, LocalSearchRequest


def payload(bundle_id):
    return {
        "bundle_id": bundle_id,
        "primary_team_id": "primary",
        "counterparty_team_ids": [],
        "min_outgoing": 1,
        "max_outgoing": 1,
        "min_incoming": 1,
        "max_incoming": 1,
        "max_total_players": 2,
        "max_imbalance": 0,
        "balanced_only": True,
        "skip_fantasypros_small_trades": False,
        "locked_player_ids": [],
        "require_no_drops": True,
        "minimum_power_delta": -100,
        "checkpoint_interval": 1,
        "scenario_count": 3,
        "seed": 19,
    }


def wait_for_job(service, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = service.job(job_id)
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(0.01)
    raise AssertionError("search job did not finish")


class LocalAppServiceTests(unittest.TestCase):
    def test_import_search_resume_and_excel_export_end_to_end(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            summary = service.import_bundle(bundle.to_record())
            request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
            estimate = service.estimate_search(request)
            started = service.start_search(request)
            finished = wait_for_job(service, started["job_id"])
            preview = service.job_results(started["job_id"])
            exported = service.export_job(started["job_id"])
            export_path = service.export_path(exported["filename"])

            resumed = service.start_search(request)
            resumed_finished = wait_for_job(service, resumed["job_id"])
            databases = tuple((Path(directory) / "searches").rglob("*.sqlite3"))

        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["positions"], ["RB"])
        self.assertEqual(
            {
                player["name"]
                for team in summary["teams"]
                for player in team["players"]
            },
            {
                bundle.player_names[player_id]
                for roster in bundle.rosters
                for player_id in roster.player_ids
            },
        )
        self.assertEqual(
            summary["methodology"]["exact_trade_scope"],
            "balanced packages with no adds or drops",
        )
        self.assertEqual(estimate["candidate_count"], 4)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["progress"]["completion_fraction"], 1)
        self.assertEqual(finished["progress"]["qualified_trade_count"], 4)
        self.assertEqual(exported["trade_count"], 4)
        self.assertEqual(preview["total_count"], 4)
        self.assertEqual(len(preview["rows"]), 4)
        self.assertEqual(
            {row["power_methodology_status"] for row in preview["rows"]},
            {"exact"},
        )
        self.assertEqual(
            {row["power_methodology_status"] for row in preview["rows"]},
            {"exact"},
        )
        self.assertEqual(len(preview["team_outlook"]), 2)
        self.assertEqual(
            {row["team_name"] for row in preview["team_outlook"]},
            {"Primary", "Other"},
        )
        self.assertTrue(
            all(0 <= row["playoff_probability"] <= 1 for row in preview["team_outlook"])
        )
        self.assertTrue(export_path.name.endswith(".xlsx"))
        self.assertEqual(resumed_finished["status"], "complete")
        self.assertEqual(len(databases), 1)

    def test_request_is_content_addressed_and_supports_explicit_roster_adjustments(self):
        bundle = engine_bundle()
        first = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        second = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        self.assertEqual(first, second)
        self.assertTrue(first.request_id.startswith("app-search_"))

        invalid = payload(bundle.bundle_id)
        invalid["require_no_drops"] = False
        invalid["max_outgoing"] = 1
        invalid["min_incoming"] = 2
        invalid["max_incoming"] = 2
        invalid["max_total_players"] = 3
        invalid["max_imbalance"] = 1
        invalid["balanced_only"] = False
        request = LocalSearchRequest.from_payload(invalid)
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            started = service.start_search(request)
            finished = wait_for_job(service, started["job_id"])
            rows = service.job_results(started["job_id"])["rows"]
        self.assertEqual(finished["status"], "complete")
        self.assertTrue(rows)
        self.assertTrue(all(row["your_drops"] for row in rows))
        self.assertEqual(
            {row["power_methodology_status"] for row in rows},
            {"extrapolated"},
        )
        self.assertTrue(
            all(row["power_methodology_status"] == "extrapolated" for row in rows)
        )

    def test_package_filters_apply_to_estimate_and_search(self):
        bundle = engine_bundle()
        filtered_payload = payload(bundle.bundle_id)
        filtered_payload.update(
            {
                "outgoing_filter": {
                    "player_ids": ["p1"],
                    "player_mode": "include",
                    "positions": [],
                    "position_mode": None,
                },
                "incoming_filter": {
                    "player_ids": ["q1"],
                    "player_mode": "only",
                    "positions": [],
                    "position_mode": None,
                },
            }
        )
        unfiltered = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        filtered = LocalSearchRequest.from_payload(filtered_payload)
        position_payload = payload(bundle.bundle_id)
        position_payload["outgoing_filter"] = {
            "player_ids": [],
            "player_mode": None,
            "positions": ["RB"],
            "position_mode": "only",
        }
        position_filtered = LocalSearchRequest.from_payload(position_payload)

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            estimate = service.estimate_search(filtered)
            position_estimate = service.estimate_search(position_filtered)
            started = service.start_search(filtered)
            finished = wait_for_job(service, started["job_id"])
            rows = service.job_results(started["job_id"])["rows"]

        self.assertEqual(estimate["candidate_count"], 1)
        self.assertEqual(position_estimate["candidate_count"], 0)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["give"], ["P1"])
        self.assertEqual(rows[0]["receive"], ["Q1"])
        self.assertNotEqual(unfiltered.request_id, filtered.request_id)
        self.assertNotIn(
            "outgoing_filter", unfiltered.to_record()["trade_constraints"]
        )
        self.assertEqual(
            filtered.to_record()["trade_constraints"][
                "package_filter_semantics_version"
            ],
            1,
        )

    def test_filter_payload_and_roster_ownership_are_validated(self):
        bundle = engine_bundle()
        malformed = payload(bundle.bundle_id)
        malformed["outgoing_filter"] = {
            "player_ids": [],
            "player_mode": "include",
            "positions": [],
            "position_mode": None,
        }
        with self.assertRaisesRegex(ValueError, "player_mode must be set exactly"):
            LocalSearchRequest.from_payload(malformed)

        wrong_side = payload(bundle.bundle_id)
        wrong_side["outgoing_filter"] = {
            "player_ids": ["q1"],
            "player_mode": "include",
            "positions": [],
            "position_mode": None,
        }
        request = LocalSearchRequest.from_payload(wrong_side)
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            with self.assertRaisesRegex(ValueError, "selected primary team"):
                service.estimate_search(request)

    def test_surrogate_requires_consent_then_searches_entirely_offline(self):
        bundle = surrogate_bundle()
        denied = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        accepted_payload = payload(bundle.bundle_id)
        accepted_payload["allow_surrogate_power"] = True
        accepted = LocalSearchRequest.from_payload(accepted_payload)
        extrapolated_payload = dict(accepted_payload)
        extrapolated_payload.update(
            {
                "balanced_only": False,
                "max_incoming": 2,
                "min_incoming": 2,
                "max_total_players": 3,
                "max_imbalance": 1,
                "require_no_drops": False,
            }
        )
        extrapolated = LocalSearchRequest.from_payload(extrapolated_payload)

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            summary = service.import_bundle(bundle.to_record())
            with self.assertRaisesRegex(ValueError, "SURROGATE"):
                service.estimate_search(denied)
            with patch(
                "trade_snapshot.browser_capture.BrowserCollector.collect",
                side_effect=AssertionError("local search must not collect provider data"),
            ):
                estimate = service.estimate_search(accepted)
                started = service.start_search(accepted)
                finished = wait_for_job(service, started["job_id"])
                results = service.job_results(started["job_id"])
                exported = service.export_job(started["job_id"])
                second = service.start_search(extrapolated)
                second_finished = wait_for_job(service, second["job_id"])
                extrapolated_results = service.job_results(second["job_id"])

        self.assertEqual(summary["power_engine_mode"], "surrogate")
        self.assertIsNone(summary["methodology"]["exact_trade_scope"])
        self.assertEqual(estimate["candidate_count"], 4)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(results["power_engine_mode"], "surrogate")
        self.assertTrue(results["rows"])
        self.assertEqual(
            {row["power_methodology_status"] for row in results["rows"]},
            {"surrogate"},
        )
        self.assertEqual(exported["trade_count"], 4)
        self.assertEqual(second_finished["status"], "complete")
        self.assertTrue(extrapolated_results["rows"])
        self.assertEqual(
            {
                row["power_methodology_status"]
                for row in extrapolated_results["rows"]
            },
            {"surrogate_extrapolated"},
        )

    def test_rejects_unknown_request_fields_and_export_traversal(self):
        bundle = engine_bundle()
        bad = payload(bundle.bundle_id)
        bad["cookie"] = "secret"
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            LocalSearchRequest.from_payload(bad)
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            with self.assertRaisesRegex(ValueError, "invalid export filename"):
                service.export_path("../secret.xlsx")


if __name__ == "__main__":
    unittest.main()
