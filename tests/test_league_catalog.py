import re
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from trade_snapshot.league_catalog import (
    CATALOG_SCHEMA_VERSION,
    LeagueCatalog,
)

ESPN_123 = "https://fantasy.espn.com/football/league?leagueId=123"
YAHOO_456 = "https://football.fantasysports.yahoo.com/f1/456"


def bundle_id(number: int) -> str:
    return f"engine_{number:064x}"


class LeagueCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "league-catalog.sqlite3"
        self.catalog = LeagueCatalog(self.path)

    def create(self, name="Home League", season=2026, scoring="PPR", **connections):
        return self.catalog.create_profile(name, season, scoring, **connections)

    def associate(self, profile_id, number=1, **changes):
        values = {
            "bundle_id": bundle_id(number),
            "season": 2026,
            "week": 5,
            "team_count": 18,
            "power_engine_mode": "holdout_validated",
            "scoring": "PPR",
        }
        values.update(changes)
        return self.catalog.associate_bundle(profile_id, **values)

    def test_create_normalizes_private_provider_identifiers_and_public_record(self):
        profile = self.create(
            espn_league_url=(
                "https://www.espn.com/fantasy/football/team?"
                "leagueId=123&seasonId=2026&teamId=9"
            ),
            yahoo_league_url=(
                "https://football.fantasysports.yahoo.com/2026/f1/456/99"
            ),
        )

        self.assertRegex(profile.profile_id, r"^league_[0-9a-f]{32}$")
        self.assertEqual(profile.espn_league_id, "123")
        self.assertEqual(profile.yahoo_league_id, "456")
        self.assertFalse(profile.archived)
        self.assertIsNone(profile.my_team_id)
        self.assertEqual(
            profile.to_record(),
            {
                "profile_id": profile.profile_id,
                "name": "Home League",
                "season": 2026,
                "scoring": "PPR",
                "espn_league_id": "123",
                "espn_collection_url": ESPN_123,
                "yahoo_league_id": "456",
                "yahoo_collection_url": (
                    "https://football.fantasysports.yahoo.com/f1/456/"
                    "players?status=ALL"
                ),
                "my_team_id": None,
                "archived": False,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            },
        )
        self.assertEqual(self.catalog.get_profile(profile.profile_id), profile)
        self.assertEqual(LeagueCatalog(self.path).get_profile(profile.profile_id), profile)

    def test_rejects_malformed_names_seasons_and_provider_pages(self):
        invalid_calls = (
            lambda: self.create(name=""),
            lambda: self.create(name=" padded "),
            lambda: self.create(name="bad\nname"),
            lambda: self.create(season=True),
            lambda: self.create(season=2011),
            lambda: self.create(scoring="ppr"),
            lambda: self.create(scoring="FULL"),
            lambda: self.create(scoring=1),
            lambda: self.create(
                espn_league_url="http://fantasy.espn.com/football/league?leagueId=1"
            ),
            lambda: self.create(
                espn_league_url=(
                    "https://fantasy.espn.com/baseball/league?leagueId=1"
                )
            ),
            lambda: self.create(
                espn_league_url=(
                    "https://fantasy.espn.com/football/league?leagueId=1&seasonId=2025"
                )
            ),
            lambda: self.create(
                yahoo_league_url=(
                    "https://football.fantasysports.yahoo.com/f1/private-key"
                )
            ),
            lambda: self.create(
                yahoo_league_url=(
                    "https://football.fantasysports.yahoo.com/2025/f1/1"
                )
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()
        self.assertEqual(self.catalog.list_profiles().total, 0)
        with self.assertRaises(TypeError):
            self.catalog.create_profile("Missing scoring", 2026)

    def test_host_connection_is_unique_but_yahoo_projection_source_is_reusable(self):
        first = self.create(
            name="First",
            espn_league_url=ESPN_123,
            yahoo_league_url=YAHOO_456,
        )
        with self.assertRaisesRegex(ValueError, "ESPN league"):
            self.create(name="Duplicate ESPN", espn_league_url=ESPN_123)
        shared_projection_source = self.create(
            name="Shared Yahoo projections",
            espn_league_url=(
                "https://fantasy.espn.com/football/league?leagueId=998"
            ),
            yahoo_league_url=YAHOO_456,
        )

        next_season = self.create(
            name="Next season",
            season=2027,
            espn_league_url=ESPN_123,
            yahoo_league_url=YAHOO_456,
        )
        second = self.create(
            name="Second",
            espn_league_url=(
                "https://fantasy.espn.com/football/league?leagueId=999"
            ),
        )
        with self.assertRaisesRegex(ValueError, "ESPN league"):
            self.catalog.update_profile(
                second.profile_id, espn_league_url=ESPN_123
            )

        self.assertEqual(self.catalog.get_profile(first.profile_id), first)
        self.assertEqual(shared_projection_source.yahoo_league_id, "456")
        self.assertEqual(next_season.season, 2027)
        self.assertEqual(self.catalog.get_profile(second.profile_id), second)

    def test_update_is_selective_and_connections_can_be_cleared(self):
        profile = self.create(
            espn_league_url=ESPN_123,
            yahoo_league_url=YAHOO_456,
        )
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.catalog.update_profile(profile.profile_id)

        updated = self.catalog.update_profile(
            profile.profile_id,
            name="Renamed League",
            scoring="HALF",
            espn_league_url=None,
        )
        self.assertEqual(updated.name, "Renamed League")
        self.assertEqual(updated.scoring, "HALF")
        self.assertIsNone(updated.espn_league_id)
        self.assertEqual(updated.yahoo_league_id, "456")
        self.assertEqual(updated.created_at, profile.created_at)
        self.assertGreaterEqual(updated.updated_at, profile.updated_at)

        moved = self.catalog.update_profile(
            profile.profile_id,
            season=2027,
            yahoo_league_url=(
                "https://football.fantasysports.yahoo.com/2027/f1/456"
            ),
        )
        self.assertEqual(moved.season, 2027)
        self.assertEqual(moved.scoring, "HALF")
        with self.assertRaisesRegex(ValueError, "scoring"):
            self.catalog.update_profile(profile.profile_id, scoring="half")
        self.assertEqual(
            LeagueCatalog(self.path).get_profile(profile.profile_id).scoring,
            "HALF",
        )

    def test_archive_hides_without_deleting_and_restore_is_idempotent(self):
        profile = self.create()
        association = self.associate(profile.profile_id)

        archived = self.catalog.archive_profile(profile.profile_id)
        self.assertTrue(archived.archived)
        self.assertTrue(self.catalog.archive_profile(profile.profile_id).archived)
        self.assertEqual(self.catalog.list_profiles().total, 0)
        included = self.catalog.list_profiles(include_archived=True)
        self.assertEqual(included.total, 1)
        self.assertEqual(included.profiles[0].profile_id, profile.profile_id)
        self.assertEqual(
            self.catalog.bundle_association(association.bundle_id), association
        )
        with self.assertRaisesRegex(ValueError, "restore"):
            self.associate(profile.profile_id, number=2)

        restored = self.catalog.restore_profile(profile.profile_id)
        self.assertFalse(restored.archived)
        self.assertFalse(self.catalog.restore_profile(profile.profile_id).archived)
        self.assertEqual(self.catalog.list_profiles().total, 1)

    def test_save_my_team_and_clear_it(self):
        profile = self.create()
        saved = self.catalog.save_my_team(profile.profile_id, "espn-team-17")
        self.assertEqual(saved.my_team_id, "espn-team-17")
        self.assertIsNone(
            self.catalog.save_my_team(profile.profile_id, None).my_team_id
        )
        for invalid in ("", " padded ", "bad\nteam", 17):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.catalog.save_my_team(profile.profile_id, invalid)
        with self.assertRaises(KeyError):
            self.catalog.save_my_team("league_" + "f" * 32, "team")

    def test_profile_listing_has_no_catalog_cap_and_uses_stable_pagination(self):
        profile_ids = {
            self.create(name=f"League {index:04d}").profile_id
            for index in range(1001)
        }
        seen = []
        cursor = None
        while True:
            page = self.catalog.list_profiles(limit=137, cursor=cursor)
            self.assertEqual(page.total, 1001)
            seen.extend(profile.profile_id for profile in page.profiles)
            cursor = page.next_cursor
            if cursor is None:
                break

        self.assertEqual(len(seen), 1001)
        self.assertEqual(set(seen), profile_ids)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(
            self.catalog.list_profiles(season=2027).to_record(),
            {"profiles": [], "total": 0, "next_cursor": None},
        )
        for kwargs in (
            {"limit": 0},
            {"limit": True},
            {"limit": 251},
            {"include_archived": 1},
            {"cursor": "not-a-cursor"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.catalog.list_profiles(**kwargs)

    def test_bundle_summary_is_local_lean_and_idempotent(self):
        profile = self.create()
        first = self.associate(profile.profile_id, number=2, week=7)
        second = self.associate(
            profile.profile_id,
            number=1,
            week=8,
            power_engine_mode="surrogate",
        )

        self.assertEqual(
            first.to_record(),
            {
                "bundle_id": bundle_id(2),
                "profile_id": profile.profile_id,
                "season": 2026,
                "week": 7,
                "team_count": 18,
                "power_engine_mode": "holdout_validated",
                "associated_at": first.associated_at,
            },
        )
        self.assertEqual(
            self.associate(profile.profile_id, number=2, week=7), first
        )
        self.assertEqual(
            self.catalog.list_bundle_ids(profile.profile_id),
            (second.bundle_id, first.bundle_id),
        )
        self.assertEqual(
            self.catalog.list_bundle_associations(profile.profile_id),
            (second, first),
        )
        self.assertEqual(
            self.catalog.associated_bundle_ids(),
            frozenset((first.bundle_id, second.bundle_id)),
        )
        self.assertIsNone(self.catalog.bundle_association(bundle_id(99)))

    def test_bundle_cannot_silently_change_owner_or_immutable_summary(self):
        source = self.create(name="Source")
        target = self.create(name="Target")
        self.associate(source.profile_id)

        with self.assertRaisesRegex(ValueError, "another league"):
            self.associate(target.profile_id)
        with self.assertRaisesRegex(ValueError, "summary"):
            self.associate(source.profile_id, team_count=20)

    def test_independent_power_bundle_is_a_supported_immutable_mode(self):
        profile = self.create()

        association = self.associate(
            profile.profile_id,
            number=17,
            power_engine_mode="independent",
        )

        self.assertEqual(association.power_engine_mode, "independent")
        self.assertEqual(
            self.catalog.bundle_association(association.bundle_id), association
        )

    def test_associated_bundle_ids_span_active_and_archived_profiles(self):
        active = self.create(name="Active")
        archived = self.create(name="Archived")
        first = self.associate(active.profile_id, number=11)
        second = self.associate(archived.profile_id, number=12)
        self.catalog.archive_profile(archived.profile_id)

        self.assertEqual(
            self.catalog.associated_bundle_ids(),
            frozenset((first.bundle_id, second.bundle_id)),
        )

    def test_rejects_malformed_bundle_summaries_and_unknown_profiles(self):
        profile = self.create()
        cases = (
            {"bundle_id": "engine_short"},
            {"season": True},
            {"season": 2027},
            {"week": 0},
            {"week": True},
            {"team_count": 1},
            {"team_count": True},
            {"power_engine_mode": "maybe"},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.associate(profile.profile_id, **changes)
        with self.assertRaises(KeyError):
            self.associate("league_" + "f" * 32)
        for bad_id in ("", "league_ABC", "league_" + "0" * 31):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.catalog.get_profile(bad_id)

    def test_cannot_change_profile_identity_or_scoring_after_bundle_association(self):
        profile = self.create(espn_league_url=ESPN_123)
        self.associate(profile.profile_id)
        with self.assertRaisesRegex(ValueError, "associated bundle"):
            self.catalog.update_profile(profile.profile_id, season=2027)
        with self.assertRaisesRegex(ValueError, "scoring cannot change"):
            self.catalog.update_profile(profile.profile_id, scoring="STD")
        with self.assertRaisesRegex(ValueError, "ESPN league cannot change"):
            self.catalog.update_profile(
                profile.profile_id,
                espn_league_url=(
                    "https://fantasy.espn.com/football/league?leagueId=999"
                ),
            )
        self.assertEqual(self.catalog.get_profile(profile.profile_id).season, 2026)

        missing_connection = self.create(name="Connection pending")
        self.associate(missing_connection.profile_id, number=2)
        connected = self.catalog.update_profile(
            missing_connection.profile_id,
            espn_league_url=(
                "https://fantasy.espn.com/football/league?leagueId=777"
            ),
        )
        self.assertEqual(connected.espn_league_id, "777")

    def test_schema_is_versioned_and_uses_a_foreign_key(self):
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                CATALOG_SCHEMA_VERSION,
            )
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(league_bundles)"
            ).fetchall()
            index_names = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(league_profiles)"
                ).fetchall()
            }
        self.assertEqual(len(foreign_keys), 1)
        self.assertEqual(foreign_keys[0][2], "league_profiles")
        self.assertTrue(
            {"league_profiles_active_page", "league_profiles_all_page"}
            <= index_names
        )

        newer_path = Path(self.temporary_directory.name) / "newer.sqlite3"
        with closing(sqlite3.connect(newer_path)) as connection:
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "newer"):
            LeagueCatalog(newer_path)

    def test_existing_version_two_catalog_migrates_exact_mode_and_indexes(self):
        profile = self.create(name="Legacy exact")
        self.associate(profile.profile_id)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP INDEX league_profiles_active_page")
            connection.execute("DROP INDEX league_profiles_all_page")
            connection.execute(
                "UPDATE league_bundles SET power_engine_mode = 'exact'"
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()

        LeagueCatalog(self.path)

        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                CATALOG_SCHEMA_VERSION,
            )
            index_names = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(league_profiles)"
                ).fetchall()
            }
            migrated_mode = connection.execute(
                "SELECT power_engine_mode FROM league_bundles"
            ).fetchone()[0]
        self.assertTrue(
            {"league_profiles_active_page", "league_profiles_all_page"}
            <= index_names
        )
        self.assertEqual(migrated_mode, "holdout_validated")

    def test_version_one_catalog_migrates_yahoo_source_to_reusable(self):
        legacy_path = Path(self.temporary_directory.name) / "version-one.sqlite3"
        LeagueCatalog(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                "CREATE UNIQUE INDEX league_profiles_yahoo "
                "ON league_profiles (season, yahoo_league_id) "
                "WHERE yahoo_league_id IS NOT NULL"
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        migrated = LeagueCatalog(legacy_path)
        first = migrated.create_profile(
            "First", 2026, "PPR", yahoo_league_url=YAHOO_456
        )
        second = migrated.create_profile(
            "Second", 2026, "PPR", yahoo_league_url=YAHOO_456
        )

        self.assertNotEqual(first.profile_id, second.profile_id)
        with closing(sqlite3.connect(legacy_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                CATALOG_SCHEMA_VERSION,
            )
            indexes = connection.execute(
                "PRAGMA index_list(league_profiles)"
            ).fetchall()
        self.assertNotIn("league_profiles_yahoo", {row[1] for row in indexes})

    def test_partial_version_zero_schema_is_completed_atomically(self):
        partial_path = Path(self.temporary_directory.name) / "partial.sqlite3"
        with closing(sqlite3.connect(partial_path)) as connection:
            connection.execute(
                """
                CREATE TABLE league_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    season INTEGER NOT NULL,
                    scoring TEXT NOT NULL
                        CHECK (scoring IN ('STD', 'HALF', 'PPR')),
                    espn_league_id TEXT,
                    yahoo_league_id TEXT,
                    my_team_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0
                        CHECK (archived IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

        recovered = LeagueCatalog(partial_path)
        profile = recovered.create_profile("Recovered", 2026, "PPR")
        recovered.associate_bundle(
            profile.profile_id,
            bundle_id=bundle_id(88),
            season=2026,
            week=1,
            team_count=12,
            power_engine_mode="holdout_validated",
            scoring="PPR",
        )
        with closing(sqlite3.connect(partial_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                CATALOG_SCHEMA_VERSION,
            )
        self.assertEqual(recovered.list_bundle_ids(profile.profile_id), (bundle_id(88),))

    def test_parallel_fresh_catalog_construction_runs_one_migration(self):
        catalogs = ()
        for attempt in range(6):
            shared_path = (
                Path(self.temporary_directory.name) / f"parallel-{attempt}.sqlite3"
            )
            barrier = Barrier(32)

            def connect(_, *, ready=barrier, path=shared_path):
                ready.wait()
                return LeagueCatalog(path)

            with ThreadPoolExecutor(max_workers=32) as executor:
                catalogs = tuple(executor.map(connect, range(32)))
        created = catalogs[0].create_profile(
            "Created after migration", 2026, "PPR"
        )
        self.assertTrue(all(
            catalog.get_profile(created.profile_id) == created for catalog in catalogs
        ))

    def test_shared_catalog_is_safe_for_concurrent_operations(self):
        def create_one(index):
            return self.create(name=f"Concurrent {index}").profile_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            profile_ids = tuple(executor.map(create_one, range(40)))
        self.assertEqual(len(profile_ids), len(set(profile_ids)))
        self.assertTrue(all(
            re.fullmatch(r"league_[0-9a-f]{32}", profile_id)
            for profile_id in profile_ids
        ))
        self.assertEqual(self.catalog.list_profiles().total, 40)


if __name__ == "__main__":
    unittest.main()
