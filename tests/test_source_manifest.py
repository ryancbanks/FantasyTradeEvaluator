import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.source_fixtures import weekly_source_manifest
from trade_snapshot.league_binding import get_or_create_league_binding
from trade_snapshot.source_manifest import WeeklySourceManifest


class SourceManifestTests(unittest.TestCase):
    def test_manifest_round_trip_is_private_and_exact(self):
        manifest = weekly_source_manifest()
        record = manifest.to_record()

        self.assertEqual(WeeklySourceManifest.from_record(record), manifest)
        self.assertNotIn("source_league_id", record)
        self.assertNotIn("leagueId", json.dumps(record))

        changed = dict(record, league_binding_id="123")
        with self.assertRaisesRegex(ValueError, "opaque local binding"):
            WeeklySourceManifest.from_record(changed)

    def test_local_catalog_reuses_one_binding_and_separates_leagues(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "league-bindings.json"
            first = get_or_create_league_binding(path, "ESPN", "123")
            repeated = get_or_create_league_binding(path, "espn", "123")
            second = get_or_create_league_binding(path, "espn", "456")

            self.assertEqual(first, repeated)
            self.assertNotEqual(first, second)
            stored = json.loads(path.read_text("utf-8"))
            self.assertEqual(len(stored["bindings"]), 2)

    def test_tampered_or_duplicate_local_catalog_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "league-bindings.json"
            path.write_text(
                '{"schema_version":1,"bindings":['
                '{"league_binding_id":"league_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                '"provider":"espn",'
                '"source_league_id":"123"},'
                '{"league_binding_id":"league_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
                '"provider":"espn",'
                '"source_league_id":"123"}]}',
                "utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate identities"):
                get_or_create_league_binding(path, "espn", "123")


if __name__ == "__main__":
    unittest.main()
