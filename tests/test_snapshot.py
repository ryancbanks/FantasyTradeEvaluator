import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trade_snapshot.model import DatasetPayload, SnapshotRequest
from trade_snapshot.snapshot import SnapshotInputError, _publish_directory, create_snapshot


FIXED_NOW = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)
TESTS_DIR = Path(__file__).resolve().parent


def write_capture(path, provider, captured_at, payload):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": provider,
                "captured_at": captured_at,
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )


class SnapshotTests(unittest.TestCase):
    def make_tempdir(self):
        return TemporaryDirectory(dir=TESTS_DIR)

    def test_imports_provider_captures_and_records_unavailable_source(self):
        with self.make_tempdir() as temp:
            root = Path(temp)
            espn = root / "espn.json"
            yahoo = root / "yahoo.json"
            write_capture(
                espn,
                "espn",
                "2026-09-01T14:00:00Z",
                {"players": [{"id": "espn-1", "projection": 18.2}]},
            )
            write_capture(
                yahoo,
                "yahoo",
                "2026-09-01T14:05:00Z",
                {"players": [{"id": "yahoo-1", "projection": 17.9}]},
            )

            result = create_snapshot(
                SnapshotRequest(season=2026, week=1, scoring="PPR"),
                root / "snapshots",
                imported_files={"espn": espn, "yahoo": yahoo},
                clock=lambda: FIXED_NOW,
            )

            manifest_path = result.path / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["request"], {"scoring": "PPR", "season": 2026, "sport": "nfl", "week": 1})
            self.assertEqual(manifest["sources"]["fantasypros"]["status"], "unavailable")
            self.assertEqual(manifest["sources"]["espn"]["status"], "available")
            self.assertEqual(manifest["sources"]["espn"]["freshness"]["state"], "unverified")
            self.assertEqual(
                manifest["sources"]["espn"]["freshness"]["source_as_of"],
                "2026-09-01T14:00:00Z",
            )
            self.assertEqual(manifest["sources"]["yahoo"]["status"], "available")
            self.assertEqual(result.failed_sources, ("fantasypros", "league_state"))
            self.assertFalse(result.ready_for_offline_compute)
            self.assertFalse(manifest["ready_for_offline_compute"])

            espn_dataset = manifest["sources"]["espn"]["datasets"][0]
            stored_bytes = (result.path / espn_dataset["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(stored_bytes).hexdigest(), espn_dataset["sha256"])
            self.assertEqual(json.loads(stored_bytes), {"players": [{"id": "espn-1", "projection": 18.2}]})
            self.assertNotIn(str(root), manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(list((root / "snapshots").glob(".*")), [])

    def test_uses_optional_official_fantasypros_fetcher_and_never_persists_key(self):
        with self.make_tempdir() as temp:
            root = Path(temp)
            observed = {}

            def fetcher(*, api_key, season, week, scoring, timeout):
                observed.update(
                    api_key=api_key,
                    season=season,
                    week=week,
                    scoring=scoring,
                    timeout=timeout,
                )
                return (
                    DatasetPayload("ecr", {"players": []}, {"endpoint": "https://example.test/ecr"}),
                    DatasetPayload("projections", {"players": []}, {"endpoint": "https://example.test/projections"}),
                    DatasetPayload("players", {"players": []}, {"endpoint": "https://example.test/players"}),
                )

            secret = "fantasypros-secret"
            result = create_snapshot(
                SnapshotRequest(season=2026, week=2, scoring="HALF"),
                root / "snapshots",
                fantasypros_api_key=secret,
                fantasypros_fetcher=fetcher,
                clock=lambda: FIXED_NOW,
            )

            manifest_text = (result.path / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(observed["api_key"], secret)
            self.assertEqual(observed["season"], 2026)
            self.assertEqual(observed["week"], 2)
            self.assertEqual(observed["scoring"], "HALF")
            self.assertNotIn(secret, manifest_text)
            self.assertEqual(manifest["sources"]["fantasypros"]["status"], "available")
            self.assertEqual(manifest["sources"]["fantasypros"]["mode"], "official_api")
            self.assertEqual(
                manifest["sources"]["fantasypros"]["freshness"]["state"],
                "unverified",
            )
            self.assertEqual(len(manifest["sources"]["fantasypros"]["datasets"]), 3)
            self.assertEqual(manifest["sources"]["espn"]["status"], "unavailable")
            self.assertEqual(manifest["sources"]["yahoo"]["status"], "unavailable")

    def test_api_failure_is_explicit_and_sanitized(self):
        with self.make_tempdir() as temp:
            root = Path(temp)

            def failing_fetcher(**kwargs):
                raise RuntimeError("provider rejected request")

            secret = "must-not-leak"
            result = create_snapshot(
                SnapshotRequest(season=2026, week=3, scoring="STD"),
                root / "snapshots",
                fantasypros_api_key=secret,
                fantasypros_fetcher=failing_fetcher,
                clock=lambda: FIXED_NOW,
            )

            manifest_text = (result.path / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            source = manifest["sources"]["fantasypros"]
            self.assertEqual(source["status"], "error")
            self.assertEqual(source["reason_code"], "fetch_failed")
            self.assertEqual(
                result.failed_sources,
                ("fantasypros", "espn", "yahoo", "league_state"),
            )
            self.assertNotIn(secret, manifest_text)
            self.assertNotIn("provider rejected request", manifest_text)

    def test_rejects_capture_for_wrong_provider_without_output(self):
        with self.make_tempdir() as temp:
            root = Path(temp)
            capture = root / "espn.json"
            write_capture(capture, "yahoo", "2026-09-01T14:00:00Z", {"players": []})

            with self.assertRaises(SnapshotInputError):
                create_snapshot(
                    SnapshotRequest(season=2026, week=1, scoring="PPR"),
                    root / "snapshots",
                    imported_files={"espn": capture},
                    clock=lambda: FIXED_NOW,
                )

            snapshots = root / "snapshots"
            self.assertFalse(snapshots.exists())

    def test_rejects_secret_like_keys_in_imported_payload(self):
        with self.make_tempdir() as temp:
            root = Path(temp)
            capture = root / "espn.json"
            write_capture(
                capture,
                "espn",
                "2026-09-01T14:00:00Z",
                {"headers": {"authorization": "Bearer secret"}, "players": []},
            )

            with self.assertRaises(SnapshotInputError):
                create_snapshot(
                    SnapshotRequest(season=2026, week=1, scoring="PPR"),
                    root / "snapshots",
                    imported_files={"espn": capture},
                    clock=lambda: FIXED_NOW,
                )

    def test_rejects_duplicate_api_dataset_names_before_writing(self):
        with self.make_tempdir() as temp:
            root = Path(temp)

            def duplicate_fetcher(**kwargs):
                return (
                    DatasetPayload("ecr", {"players": []}),
                    DatasetPayload("ecr", {"players": [{"id": 1}]}),
                    DatasetPayload("projections", {"players": []}),
                    DatasetPayload("players", {"players": []}),
                )

            with self.assertRaises(ValueError):
                create_snapshot(
                    SnapshotRequest(season=2026, week=1, scoring="PPR"),
                    root / "snapshots",
                    fantasypros_api_key="not-persisted",
                    fantasypros_fetcher=duplicate_fetcher,
                    clock=lambda: FIXED_NOW,
                )

            self.assertFalse((root / "snapshots").exists())


class SnapshotRequestTests(unittest.TestCase):
    def test_validates_week_and_scoring(self):
        with self.assertRaises(ValueError):
            SnapshotRequest(season=2026, week=-1, scoring="PPR")
        with self.assertRaises(ValueError):
            SnapshotRequest(season=2026, week=1, scoring="full")


class SnapshotPublishTests(unittest.TestCase):
    def test_retries_transient_windows_directory_lock(self):
        with TemporaryDirectory(dir=TESTS_DIR) as temp:
            root = Path(temp)
            stage = root / "stage"
            final = root / "final"
            stage.mkdir()
            calls = 0

            def flaky_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise PermissionError("transient lock")
                source.rename(destination)

            with patch("trade_snapshot.snapshot.os.replace", side_effect=flaky_replace), patch(
                "trade_snapshot.snapshot.time.sleep"
            ) as sleep:
                _publish_directory(stage, final)

            self.assertEqual(calls, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(final.is_dir())


if __name__ == "__main__":
    unittest.main()
