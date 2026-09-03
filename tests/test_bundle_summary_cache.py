import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot.bundle_summary_cache import (
    BundleSummaryCacheError,
    load_cached_bundle_summary,
    save_bundle_with_summary,
)
from trade_snapshot.engine_bundle import save_engine_bundle
from trade_snapshot.engine_bundle import CANONICAL_ENGINE_BUNDLE_REVISION


class BundleSummaryCacheTests(unittest.TestCase):
    def test_saves_and_loads_a_content_checked_summary(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            bundle_path = Path(directory) / "bundles" / f"{bundle.bundle_id}.json"

            saved = save_bundle_with_summary(bundle, bundle_path)
            summary = load_cached_bundle_summary(saved)

            self.assertEqual(summary["bundle_id"], bundle.bundle_id)
            cache_files = tuple((bundle_path.parent / ".summaries").glob("*.json"))
            self.assertEqual(len(cache_files), 1)
            record = json.loads(cache_files[0].read_text(encoding="utf-8"))
            self.assertEqual(record["bundle_id"], bundle.bundle_id)
            self.assertEqual(
                record["engine_bundle_revision"],
                CANONICAL_ENGINE_BUNDLE_REVISION,
            )
            self.assertEqual(record["summary"]["status"], "ready")

    def test_missing_or_stale_summary_is_not_reused(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            bundle_path = Path(directory) / f"{bundle.bundle_id}.json"
            save_engine_bundle(bundle, bundle_path)
            self.assertIsNone(load_cached_bundle_summary(bundle_path))

            save_bundle_with_summary(bundle, bundle_path)
            bundle_path.write_text("{}", encoding="utf-8")
            self.assertIsNone(load_cached_bundle_summary(bundle_path))

    def test_rejects_a_tampered_or_non_unique_summary(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            bundle_path = Path(directory) / f"{bundle.bundle_id}.json"
            save_bundle_with_summary(bundle, bundle_path)
            cache_path = bundle_path.parent / ".summaries" / f"{bundle.bundle_id}.json"
            record = json.loads(cache_path.read_text(encoding="utf-8"))
            record["summary"]["season"] += 1
            cache_path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(BundleSummaryCacheError, "content"):
                load_cached_bundle_summary(bundle_path)

            cache_path.write_text('{"kind":"one","kind":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(BundleSummaryCacheError, "duplicate|invalid"):
                load_cached_bundle_summary(bundle_path)


if __name__ == "__main__":
    unittest.main()
