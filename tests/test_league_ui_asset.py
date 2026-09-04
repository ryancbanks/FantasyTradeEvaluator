import unittest
from pathlib import Path

ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "trade_snapshot"
    / "web_assets"
    / "league_ui.js"
)
ASSET_SOURCE = ASSET_PATH.read_text(encoding="utf-8")


class LeagueUiStaticContractTests(unittest.TestCase):
    def test_profile_list_is_cursor_paginated_without_a_ui_count_cap(self):
        self.assertIn('limit: "200"', ASSET_SOURCE)
        self.assertIn("page.next_cursor", ASSET_SOURCE)
        self.assertIn("seenCursors", ASSET_SOURCE)
        self.assertIn("rows.push(...page.profiles)", ASSET_SOURCE)
        self.assertIn("page.unassigned_bundle_count !== undefined", ASSET_SOURCE)
        self.assertNotIn("profiles.slice(", ASSET_SOURCE)
        self.assertNotIn("MAX_LEAGUE", ASSET_SOURCE)

    def test_module_uses_scoped_routes_and_safe_dom_writes(self):
        for route in (
            '"/api/leagues"',
            "/api/leagues?",
            "/bundles/import",
            "/team",
            "/assign",
        ):
            with self.subTest(route=route):
                self.assertIn(route, ASSET_SOURCE)
        self.assertIn("textContent", ASSET_SOURCE)
        self.assertIn("replaceChildren", ASSET_SOURCE)
        self.assertIn("if (busy) closeEditor()", ASSET_SOURCE)
        self.assertIn('"assignBundleButton"', ASSET_SOURCE)
        self.assertIn("use_broad_consensus", ASSET_SOURCE)
        self.assertIn("refresh_public_player_data", ASSET_SOURCE)
        for unsafe_sink in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "eval(",
            "new Function",
        ):
            with self.subTest(unsafe_sink=unsafe_sink):
                self.assertNotIn(unsafe_sink, ASSET_SOURCE)


if __name__ == "__main__":
    unittest.main()
