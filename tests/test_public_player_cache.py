import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_player_profiles import _public_data
from trade_snapshot.public_player_cache import (
    PublicPlayerCacheError,
    load_public_player_week,
    save_public_player_week,
)


class PublicPlayerCacheTests(unittest.TestCase):
    def test_round_trips_one_content_verified_week(self):
        snapshot = _public_data()
        with TemporaryDirectory() as directory:
            self.assertIsNone(load_public_player_week(directory, 2026, 1))

            path = save_public_player_week(directory, 2026, 1, snapshot)

            self.assertEqual(path.name, "2026-week-01.json.gz")
            self.assertEqual(load_public_player_week(directory, 2026, 1), snapshot)
            self.assertIsNone(load_public_player_week(directory, 2026, 2))

    def test_rejects_tampering_and_wrong_cache_context(self):
        with TemporaryDirectory() as directory:
            path = save_public_player_week(directory, 2026, 1, _public_data())
            record = json.loads(gzip.decompress(path.read_bytes()))
            record["week"] = 2
            path.write_bytes(gzip.compress(json.dumps(record).encode("utf-8")))
            with self.assertRaisesRegex(PublicPlayerCacheError, "context"):
                load_public_player_week(directory, 2026, 1)

            record["week"] = 1
            record["player_data"]["data_id"] = "public_player_data_" + "0" * 64
            path.write_bytes(gzip.compress(json.dumps(record).encode("utf-8")))
            with self.assertRaisesRegex(PublicPlayerCacheError, "invalid"):
                load_public_player_week(directory, 2026, 1)

    def test_rejects_non_files_and_invalid_keys(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "public-player-cache" / "2026-week-01.json.gz"
            path.mkdir(parents=True)
            with self.assertRaisesRegex(PublicPlayerCacheError, "not a file"):
                load_public_player_week(directory, 2026, 1)
        with self.assertRaisesRegex(PublicPlayerCacheError, "week"):
            load_public_player_week(".", 2026, 0)

    def test_rejects_boolean_schema_and_week_values(self):
        with TemporaryDirectory() as directory:
            path = save_public_player_week(directory, 2026, 1, _public_data())
            original = json.loads(gzip.decompress(path.read_bytes()))
            for field in ("schema_version", "week"):
                with self.subTest(field=field):
                    record = dict(original)
                    record[field] = True
                    path.write_bytes(
                        gzip.compress(json.dumps(record).encode("utf-8"))
                    )
                    with self.assertRaisesRegex(PublicPlayerCacheError, "context"):
                        load_public_player_week(directory, 2026, 1)


if __name__ == "__main__":
    unittest.main()
