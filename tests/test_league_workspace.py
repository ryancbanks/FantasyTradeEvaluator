from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import json
import time
import unittest

from tests.test_engine_bundle import engine_bundle
from tests.test_weekly_collection import SuccessfulWorkflow
from trade_snapshot.app_service import LocalAppService
from trade_snapshot.league_workspace import _bundle_scoring
from trade_snapshot.scoring import ScoringProfile


def profile_payload(name="Home League"):
    return {
        "name": name,
        "season": 2026,
        "scoring": "PPR",
        "host_league_url": (
            "https://fantasy.espn.com/football/league?leagueId=123"
        ),
        "yahoo_projection_league_url": (
            "https://football.fantasysports.yahoo.com/f1/456/players"
        ),
    }


def wait_for_collection(service, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = service.weekly_collection(job_id)
        if record["status"] not in {"queued", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("weekly collection did not finish")


class BlockingSuccessfulWorkflow(SuccessfulWorkflow):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()

    def __call__(self, request, *, data_directory, progress, cancelled):
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("test did not release weekly collection")
        return super().__call__(
            request,
            data_directory=data_directory,
            progress=progress,
            cancelled=cancelled,
        )


class LeagueWorkspaceServiceTests(unittest.TestCase):
    def test_production_espn_scoring_shape_maps_to_workspace_modes(self):
        for rank_type, expected in (
            ("STANDARD", "STD"),
            ("HALF_PPR", "HALF"),
            ("PPR", "PPR"),
        ):
            with self.subTest(rank_type=rank_type):
                bundle = SimpleNamespace(scoring_profile=ScoringProfile(
                    "espn",
                    {
                        "adapter_version": 4,
                        "scoring_settings": {"playerRankType": rank_type},
                    },
                ))
                self.assertEqual(_bundle_scoring(bundle), expected)

    def test_unassigned_count_is_computed_only_on_first_profile_page(self):
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.create_league_profile(profile_payload("First"))
            second_payload = profile_payload("Second")
            second_payload["host_league_url"] = (
                "https://fantasy.espn.com/football/league?leagueId=999"
            )
            service.create_league_profile(second_payload)

            first = service.league_profiles(limit=1)
            second = service.league_profiles(limit=1, cursor=first["next_cursor"])

        self.assertIn("unassigned_bundle_count", first)
        self.assertNotIn("unassigned_bundle_count", second)

    def test_profiles_keep_weekly_bundles_separate_without_changing_bundle_data(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            profile = service.create_league_profile(profile_payload())
            imported = service.import_bundle(
                bundle.to_record(), league_profile_id=profile["profile_id"]
            )
            profile_catalog = service.league_bundle_catalog(profile["profile_id"])
            unassigned_catalog = service.league_bundle_catalog("unassigned")
            full_bundle = service.bundle(bundle.bundle_id)
            saved = service.save_league_team(
                profile["profile_id"],
                {"bundle_id": bundle.bundle_id, "team_id": "primary"},
            )

        self.assertEqual(imported["bundle_id"], bundle.bundle_id)
        self.assertEqual(
            profile_catalog["bundles"],
            ({
                "bundle_id": bundle.bundle_id,
                "profile_id": profile["profile_id"],
                "season": 2026,
                "week": 1,
                "team_count": 2,
                "power_engine_mode": "exact",
                "associated_at": profile_catalog["bundles"][0]["associated_at"],
                "status": "ready",
            },),
        )
        self.assertEqual(unassigned_catalog["bundles"], ())
        self.assertEqual(full_bundle["teams"], imported["teams"])
        self.assertEqual(saved["my_team_id"], "primary")

    def test_unassigned_import_can_be_assigned_to_a_matching_league(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            profile = service.create_league_profile(profile_payload())
            service.import_bundle(bundle.to_record())

            before = service.league_bundle_catalog("unassigned")
            association = service.assign_bundle_to_league(
                profile["profile_id"], bundle.bundle_id
            )
            after = service.league_bundle_catalog("unassigned")

        self.assertEqual(before["bundles"][0]["bundle_id"], bundle.bundle_id)
        self.assertFalse(before["readiness"]["collection_available"])
        self.assertEqual(association["profile_id"], profile["profile_id"])
        self.assertEqual(after["bundles"], ())

    def test_profile_collection_uses_an_isolated_workspace_and_auto_associates(self):
        bundle = engine_bundle()
        workflow = SuccessfulWorkflow()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula = root / "methodology" / "strength-formula.json"
            formula.parent.mkdir(parents=True)
            formula.write_text(json.dumps({"formula": "legacy exact"}), encoding="utf-8")
            service = LocalAppService(
                directory, weekly_collection_workflow=workflow
            )
            profile = service.create_league_profile(profile_payload())
            started = service.start_profile_weekly_collection(
                profile["profile_id"],
                {
                    "week": 1,
                    "include_future_weekly": False,
                    "allow_surrogate_power": False,
                },
            )
            finished = wait_for_collection(service, started["job_id"])
            catalog = service.league_bundle_catalog(profile["profile_id"])
            workspace = root / "leagues" / profile["profile_id"]

            self.assertEqual(finished["status"], "complete", finished)
            self.assertEqual(workflow.calls[0][1], workspace.resolve())
            self.assertEqual(
                json.loads(
                    (workspace / "methodology" / "strength-formula.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {"formula": "legacy exact"},
            )
            self.assertEqual(catalog["bundles"][0]["bundle_id"], bundle.bundle_id)
            self.assertEqual(catalog["bundles"][0]["status"], "ready")

    def test_collection_cannot_publish_into_a_profile_whose_espn_id_changed(self):
        bundle = engine_bundle()
        workflow = BlockingSuccessfulWorkflow()
        with TemporaryDirectory() as directory:
            service = LocalAppService(
                directory, weekly_collection_workflow=workflow
            )
            profile = service.create_league_profile(profile_payload())
            started = service.start_profile_weekly_collection(
                profile["profile_id"],
                {
                    "week": 1,
                    "include_future_weekly": False,
                    "allow_surrogate_power": False,
                },
            )
            self.assertTrue(workflow.started.wait(1))
            service.update_league_profile(
                profile["profile_id"],
                {
                    "host_league_url": (
                        "https://fantasy.espn.com/football/league?leagueId=999"
                    )
                },
            )
            workflow.release.set()
            finished = wait_for_collection(service, started["job_id"])
            profile_catalog = service.league_bundle_catalog(profile["profile_id"])
            unassigned = service.league_bundle_catalog("unassigned")

        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["bundle_id"], bundle.bundle_id)
        self.assertEqual(profile_catalog["bundles"], ())
        self.assertEqual(unassigned["bundles"][0]["bundle_id"], bundle.bundle_id)
        self.assertIn("saved under Unassigned", finished["error"])

    def test_profile_payloads_and_team_ownership_are_strict(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            profile = service.create_league_profile(profile_payload())
            service.import_bundle(bundle.to_record())

            with self.assertRaisesRegex(ValueError, "fields are invalid"):
                service.create_league_profile(dict(profile_payload(), cookie="secret"))
            with self.assertRaisesRegex(ValueError, "does not belong"):
                service.save_league_team(
                    profile["profile_id"],
                    {"bundle_id": bundle.bundle_id, "team_id": "primary"},
                )
            with self.assertRaisesRegex(ValueError, "fields are invalid"):
                service.start_profile_weekly_collection(
                    profile["profile_id"], {"week": 1}
                )

    def test_invalid_profile_import_does_not_publish_a_bundle_file(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            future = profile_payload("Future League")
            future.update(
                season=2027,
                host_league_url=(
                    "https://fantasy.espn.com/football/league?leagueId=999"
                ),
                yahoo_projection_league_url=(
                    "https://football.fantasysports.yahoo.com/f1/888/players"
                ),
            )
            profile = service.create_league_profile(future)

            with self.assertRaisesRegex(ValueError, "season must match"):
                service.import_bundle(
                    bundle.to_record(), league_profile_id=profile["profile_id"]
                )

            self.assertFalse(
                (Path(directory) / "bundles" / f"{bundle.bundle_id}.json").exists()
            )

    def test_scoring_mismatch_cannot_be_imported_or_assigned(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            standard = profile_payload("Standard League")
            standard["scoring"] = "STD"
            profile = service.create_league_profile(standard)

            with self.assertRaisesRegex(ValueError, "reception scoring must match"):
                service.import_bundle(
                    bundle.to_record(), league_profile_id=profile["profile_id"]
                )
            path = Path(directory) / "bundles" / f"{bundle.bundle_id}.json"
            self.assertFalse(path.exists())

            service.import_bundle(bundle.to_record())
            with self.assertRaisesRegex(ValueError, "reception scoring must match"):
                service.assign_bundle_to_league(
                    profile["profile_id"], bundle.bundle_id
                )
            self.assertEqual(
                service.league_bundle_catalog("unassigned")["bundles"][0]["bundle_id"],
                bundle.bundle_id,
            )

    def test_damaged_associated_bundle_is_not_advertised_as_ready(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            profile = service.create_league_profile(profile_payload())
            service.import_bundle(
                bundle.to_record(), league_profile_id=profile["profile_id"]
            )
            path = Path(directory) / "bundles" / f"{bundle.bundle_id}.json"
            path.write_text("{}", encoding="utf-8")

            catalog = service.league_bundle_catalog(profile["profile_id"])

        self.assertEqual(catalog["bundles"][0]["status"], "invalid")
        self.assertFalse(catalog["readiness"]["ready"])
        self.assertIn("failed validation", catalog["readiness"]["message"])


if __name__ == "__main__":
    unittest.main()
