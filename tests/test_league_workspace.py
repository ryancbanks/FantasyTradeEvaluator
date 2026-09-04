import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.test_engine_bundle import engine_bundle
from tests.test_independent_weekly_assembly import projection_artifacts
from tests.test_weekly_assembly import host_snapshot, nfl_schedule
from trade_snapshot.independent_weekly_assembly import (
    assemble_independent_weekly_engine,
)
from trade_snapshot.league_workspace import (
    UNASSIGNED_PROFILE_ID,
    LeagueWorkspaceService,
    _bundle_scoring,
)
from trade_snapshot.scoring import ScoringProfile


def profile_payload(name="Home League", *, host_url=None):
    return {
        "name": name,
        "season": 2026,
        "scoring": "PPR",
        "host_league_url": (
            host_url
            if host_url is not None
            else "https://fantasy.espn.com/football/league?leagueId=123"
        ),
        "yahoo_projection_league_url": (
            "https://football.fantasysports.yahoo.com/f1/456/players?"
            "&pos=O&sort=OR&stat1=S_PS_2026"
        ),
    }


def workspace_service(directory):
    root = Path(directory)
    return LeagueWorkspaceService(root, root / "bundles")


def collection_payload(**changes):
    payload = {
        "week": 1,
        "include_future_weekly": False,
        "allow_surrogate_power": False,
        "use_fantasypros": True,
        "use_broad_consensus": True,
        "refresh_public_player_data": False,
    }
    payload.update(changes)
    return payload


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
            service = workspace_service(directory)
            service.create_profile(profile_payload("First"))
            second = profile_payload(
                "Second",
                host_url=(
                    "https://fantasy.espn.com/football/league?leagueId=999"
                ),
            )
            service.create_profile(second)

            first_page = service.profiles(
                include_archived=False, limit=1, cursor=None
            )
            second_page = service.profiles(
                include_archived=False,
                limit=1,
                cursor=first_page["next_cursor"],
            )

        self.assertIn("unassigned_bundle_count", first_page)
        self.assertNotIn("unassigned_bundle_count", second_page)

    def test_profiles_keep_bundles_separate_without_changing_bundle_data(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())
            imported = service.import_bundle(
                bundle.to_record(), profile_id=profile["profile_id"]
            )
            owning_data_directory = service.data_directory_for_bundle(
                bundle.bundle_id
            )
            profile_rows = service.bundle_rows(profile["profile_id"])
            unassigned_rows = service.bundle_rows(UNASSIGNED_PROFILE_ID)
            full_bundle = service.full_bundle(bundle.bundle_id)
            saved = service.save_my_team(
                profile["profile_id"],
                bundle_id=bundle.bundle_id,
                team_id="primary",
            )
            summary_cache = (
                Path(directory)
                / "bundles"
                / ".summaries"
                / f"{bundle.bundle_id}.json"
            )
            summary_cache_exists = summary_cache.is_file()

        self.assertEqual(imported["bundle_id"], bundle.bundle_id)
        self.assertEqual(
            owning_data_directory,
            (Path(directory) / "leagues" / profile["profile_id"]).resolve(),
        )
        self.assertEqual(
            profile_rows,
            ({
                "bundle_id": bundle.bundle_id,
                "profile_id": profile["profile_id"],
                "season": 2026,
                "week": 1,
                "team_count": 2,
                "power_engine_mode": "exact",
                "associated_at": profile_rows[0]["associated_at"],
                "status": "ready",
            },),
        )
        self.assertEqual(unassigned_rows, ())
        self.assertEqual(full_bundle["teams"], imported["teams"])
        self.assertEqual(saved["my_team_id"], "primary")
        self.assertTrue(summary_cache_exists)

    def test_unassigned_import_can_be_assigned_to_a_matching_league(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())
            service.import_bundle(bundle.to_record())

            before = service.bundle_rows(UNASSIGNED_PROFILE_ID)
            association = service.assign_bundle(
                profile["profile_id"], bundle.bundle_id
            )
            after = service.bundle_rows(UNASSIGNED_PROFILE_ID)

        self.assertEqual(before[0]["bundle_id"], bundle.bundle_id)
        self.assertEqual(association["profile_id"], profile["profile_id"])
        self.assertEqual(after, ())

    def test_independent_espn_yahoo_bundle_round_trips_in_a_workspace(self):
        bundle = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=projection_artifacts(broad=False),
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
        ).bundle
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())

            imported = service.import_bundle(
                bundle.to_record(), profile_id=profile["profile_id"]
            )
            rows = service.bundle_rows(profile["profile_id"])
            reloaded = service.full_bundle(bundle.bundle_id)

        self.assertEqual(imported["power_engine_mode"], "independent")
        self.assertEqual(rows[0]["power_engine_mode"], "independent")
        self.assertEqual(reloaded["bundle_id"], bundle.bundle_id)

    def test_collection_plan_uses_isolated_workspace_and_current_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula = root / "methodology" / "strength-formula.json"
            formula.parent.mkdir(parents=True)
            formula.write_text(
                json.dumps({"formula": "legacy exact"}), encoding="utf-8"
            )
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())

            plan = service.collection_plan(
                profile["profile_id"], collection_payload()
            )

            expected_workspace = (
                root / "leagues" / profile["profile_id"]
            ).resolve()
            self.assertEqual(plan.workspace, expected_workspace)
            self.assertEqual(plan.espn_league_id, "123")
            self.assertEqual(plan.request.host_league_url, (
                "https://fantasy.espn.com/football/league?leagueId=123"
            ))
            self.assertEqual(plan.request.yahoo_projection_league_url, (
                "https://football.fantasysports.yahoo.com/f1/456/"
                "players?status=ALL"
            ))
            self.assertTrue(plan.request.use_fantasypros)
            self.assertTrue(plan.request.use_broad_consensus)
            self.assertFalse(plan.request.refresh_public_player_data)
            self.assertEqual(
                json.loads(
                    (
                        expected_workspace
                        / "methodology"
                        / "strength-formula.json"
                    ).read_text(encoding="utf-8")
                ),
                {"formula": "legacy exact"},
            )

    def test_collection_plan_keeps_old_payload_compatible(self):
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())
            plan = service.collection_plan(
                profile["profile_id"],
                {
                    "week": 1,
                    "include_future_weekly": False,
                    "allow_surrogate_power": False,
                },
            )

        self.assertTrue(plan.request.use_fantasypros)
        self.assertFalse(plan.request.use_broad_consensus)
        self.assertFalse(plan.request.refresh_public_player_data)

    def test_independent_collection_requires_espn_but_fantasypros_does_not(self):
        payload = profile_payload()
        payload["host_league_url"] = None
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(payload)

            fantasypros_plan = service.collection_plan(
                profile["profile_id"], collection_payload(use_fantasypros=True)
            )
            with self.assertRaisesRegex(ValueError, "ESPN connection"):
                service.collection_plan(
                    profile["profile_id"],
                    collection_payload(use_fantasypros=False),
                )

        self.assertIsNone(fantasypros_plan.request.host_league_url)
        self.assertIsNone(fantasypros_plan.espn_league_id)

    def test_collection_cannot_associate_after_espn_connection_changes(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())
            plan = service.collection_plan(
                profile["profile_id"], collection_payload()
            )
            service.import_bundle(bundle.to_record())
            service.update_profile(
                profile["profile_id"],
                {
                    "host_league_url": (
                        "https://fantasy.espn.com/football/league?leagueId=999"
                    )
                },
            )

            with self.assertRaisesRegex(ValueError, "changed while"):
                service.associate_bundle(
                    profile["profile_id"],
                    bundle,
                    expected_espn_league_id=plan.espn_league_id,
                )

            self.assertEqual(
                service.bundle_rows(UNASSIGNED_PROFILE_ID)[0]["bundle_id"],
                bundle.bundle_id,
            )

    def test_profile_payloads_collection_fields_and_team_ownership_are_strict(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())
            service.import_bundle(bundle.to_record())

            with self.assertRaisesRegex(ValueError, "fields are invalid"):
                service.create_profile(dict(profile_payload(), cookie="secret"))
            with self.assertRaisesRegex(ValueError, "does not belong"):
                service.save_my_team(
                    profile["profile_id"],
                    bundle_id=bundle.bundle_id,
                    team_id="primary",
                )
            with self.assertRaisesRegex(ValueError, "fields are invalid"):
                service.collection_plan(profile["profile_id"], {"week": 1})
            with self.assertRaisesRegex(ValueError, "fields are invalid"):
                service.collection_plan(
                    profile["profile_id"],
                    dict(collection_payload(), cookie="secret"),
                )
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                service.collection_plan(
                    profile["profile_id"],
                    collection_payload(refresh_public_player_data=1),
                )

    def test_invalid_profile_import_does_not_publish_a_bundle_file(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            future = profile_payload(
                "Future League",
                host_url=(
                    "https://fantasy.espn.com/football/league?"
                    "leagueId=999&seasonId=2027"
                ),
            )
            future.update(
                season=2027,
                yahoo_projection_league_url=(
                    "https://football.fantasysports.yahoo.com/2027/f1/888/players"
                ),
            )
            profile = service.create_profile(future)

            with self.assertRaisesRegex(ValueError, "season must match"):
                service.import_bundle(
                    bundle.to_record(), profile_id=profile["profile_id"]
                )

            self.assertFalse(
                (
                    Path(directory)
                    / "bundles"
                    / f"{bundle.bundle_id}.json"
                ).exists()
            )

    def test_scoring_mismatch_cannot_be_imported_or_assigned(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            standard = profile_payload("Standard League")
            standard["scoring"] = "STD"
            profile = service.create_profile(standard)

            with self.assertRaisesRegex(ValueError, "reception scoring must match"):
                service.import_bundle(
                    bundle.to_record(), profile_id=profile["profile_id"]
                )
            path = (
                Path(directory) / "bundles" / f"{bundle.bundle_id}.json"
            )
            self.assertFalse(path.exists())

            service.import_bundle(bundle.to_record())
            with self.assertRaisesRegex(ValueError, "reception scoring must match"):
                service.assign_bundle(profile["profile_id"], bundle.bundle_id)
            self.assertEqual(
                service.bundle_rows(UNASSIGNED_PROFILE_ID)[0]["bundle_id"],
                bundle.bundle_id,
            )

    def test_damaged_associated_bundle_is_not_advertised_as_ready(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = workspace_service(directory)
            profile = service.create_profile(profile_payload())
            service.import_bundle(
                bundle.to_record(), profile_id=profile["profile_id"]
            )
            path = (
                Path(directory) / "bundles" / f"{bundle.bundle_id}.json"
            )
            path.write_text("{}", encoding="utf-8")

            rows = service.bundle_rows(profile["profile_id"])

        self.assertEqual(rows[0]["status"], "invalid")
        self.assertIn("missing, damaged", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
