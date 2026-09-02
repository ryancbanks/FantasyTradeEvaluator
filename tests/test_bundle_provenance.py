from hashlib import sha256
import unittest
from unittest.mock import patch

from trade_snapshot.bundle_provenance import (
    analyzer_response_schema_sha256,
    discover_analyzer_bundle_url,
    fetch_analyzer_bundle_fingerprint,
    fingerprint_analyzer_bundle,
)


URL = (
    "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
    "trade-analyzer/bundle-abc123.js"
)


class BundleProvenanceTests(unittest.TestCase):
    def test_discovers_one_exact_public_analyzer_bundle(self):
        html = f"""
        <script src="https://cdn.fantasypros.com/assets/js/vendor.js"></script>
        <script src="{URL.removeprefix('https:')}"></script>
        <script src="{URL}"></script>
        """
        self.assertEqual(discover_analyzer_bundle_url(html), URL)

    def test_rejects_missing_ambiguous_relative_or_query_bearing_sources(self):
        bad = (
            "<script src='/assets/js/min/pages/myplaybook/trade-analyzer/"
            "bundle-abc123.js'></script>"
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            discover_analyzer_bundle_url(bad)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            discover_analyzer_bundle_url(
                f'<script src="{URL}"></script>'
                f'<script src="{URL.replace("abc123", "def456")}"></script>'
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            discover_analyzer_bundle_url(f'<script src="{URL}?key=private"></script>')

    def test_hashes_exact_bytes_and_fingerprints_response_policy(self):
        content = b"x" * 2048
        value = fingerprint_analyzer_bundle(URL, content)
        self.assertEqual(value.sha256, sha256(content).hexdigest())
        schema = analyzer_response_schema_sha256()
        self.assertEqual(len(schema), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in schema))

    def test_rejects_non_analyzer_url_or_tiny_payload(self):
        with self.assertRaises(ValueError):
            fingerprint_analyzer_bundle(
                "https://cdn.fantasypros.com/assets/js/vendor.js",
                b"x" * 2048,
            )
        with self.assertRaises(ValueError):
            fingerprint_analyzer_bundle(URL, b"x")

    def test_fetch_rejects_non_allowlisted_origin_before_network_access(self):
        with patch("trade_snapshot.bundle_provenance.urlopen") as opener:
            with self.assertRaises(ValueError):
                fetch_analyzer_bundle_fingerprint(
                    "https://evil.test/assets/js/min/pages/myplaybook/"
                    "trade-analyzer/bundle-abc123.js"
                )
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
