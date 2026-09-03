from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "trade_snapshot" / "web_assets"


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])


class PlayerLabAssetTests(unittest.TestCase):
    def setUp(self):
        self.page = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
        self.app_script = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
        self.script = (ASSET_ROOT / "player_lab_ui.js").read_text(encoding="utf-8")
        self.catalog_script = (ASSET_ROOT / "player_lab_catalog_ui.js").read_text(encoding="utf-8")
        self.profile_script = (ASSET_ROOT / "player_lab_profile_ui.js").read_text(encoding="utf-8")
        self.styles = (ASSET_ROOT / "player_lab.css").read_text(encoding="utf-8")

    def test_every_player_lab_control_is_present_and_bound(self):
        parser = _IdCollector()
        parser.feed(self.page)
        controls = {
            "playerLabSearch",
            "playerLabOwnerFilter",
            "playerLabNflTeamFilter",
            "playerLabPositionFilter",
            "playerLabGroup",
            "playerLabTrendFilter",
            "playerLabProjectionMin",
            "playerLabProjectionMax",
            "playerLabSort",
        }
        self.assertTrue(controls <= parser.ids)
        for control in controls:
            with self.subTest(control=control):
                self.assertIn(control, self.script + self.catalog_script)
        self.assertIn("FILTER_CONTROL_EVENTS", self.script)
        for value in ("performance:rising", "performance:unknown", "market:rising", "market:unknown"):
            self.assertIn(f'value="{value}"', self.page)
        self.assertIn("description.marketTrend?.direction", self.catalog_script)
        self.assertIn("description.performanceTrend.direction", self.catalog_script)

    def test_profile_ui_has_required_evidence_and_accessibility_language(self):
        for phrase in (
            "Documented injury-report history",
            "not a medical prediction",
            "probability of future injury",
            "documented report",
            "Trending data via Sleeper",
            "createElementNS",
            "player-lab-chart-table",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.profile_script)
        self.assertIn("player-lab-availability", self.styles)
        self.assertIn("player-lab-chart-svg", self.styles)
        self.assertIn("prefers-reduced-motion", self.styles)
        self.assertIn("fantasy_points_selected", self.profile_script)
        self.assertIn("scoring_mode", self.profile_script)
        self.assertNotIn("innerHTML", self.script + self.catalog_script + self.profile_script)

    def test_player_lab_script_has_valid_javascript_syntax_when_node_is_available(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed in this source-test environment")
        for name in ("player_lab_profile_ui.js", "player_lab_catalog_ui.js", "player_lab_ui.js"):
            with self.subTest(name=name):
                result = subprocess.run(
                    [node, "--check", str(ASSET_ROOT / name)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_profile_module_loads_before_the_player_lab_controller(self):
        profile_index = self.page.index('src="/player_lab_profile_ui.js"')
        catalog_index = self.page.index('src="/player_lab_catalog_ui.js"')
        controller_index = self.page.index('src="/player_lab_ui.js"')
        self.assertLess(profile_index, catalog_index)
        self.assertLess(catalog_index, controller_index)
        self.assertIn("window.PlayerLabCatalogUi.create", self.script)
        self.assertIn("window.PlayerLabProfileUi.describe", self.script)
        self.assertIn("window.PlayerLabProfileUi.render", self.script)

    def test_outlook_is_lazy_and_master_rows_are_paginated(self):
        set_bundle = self.script[
            self.script.index("function queueBundle("):
            self.script.index("async function loadPendingBundle(")
        ]
        self.assertNotIn("options.request", set_bundle)
        self.assertIn('activeWorkspace === "players" ? loadPendingBundle()', set_bundle)
        self.assertIn("void loadPendingBundle()", self.script)
        self.assertIn("PlayerLabUi.queueBundle(currentBundle()", self.app_script)
        self.assertIn("players.slice(firstIndex, firstIndex + size)", self.catalog_script)
        self.assertIn("catalogUi.updateSelection(previousId, playerId)", self.script)
        self.assertIn("player-outlook/players/${encodeURIComponent(playerId)}", self.script)
        self.assertIn("loadingDetailPlayerId === playerId && detailPromise", self.script)
        self.assertIn("DETAIL_CACHE_LIMIT = 12", self.script)
        self.assertIn("cancelDetailRequest()", self.script)
        for control in (
            "playerLabPreviousPage", "playerLabPageStatus",
            "playerLabNextPage", "playerLabPageSize",
        ):
            self.assertIn(f'id="{control}"', self.page)
            self.assertIn(f'$("{control}")', self.script)

    def test_stats_and_chart_copy_match_the_retained_public_contract(self):
        self.assertIn('"passing_interceptions"', self.profile_script)
        self.assertIn('"def_tackles_solo"', self.profile_script)
        self.assertIn('"fg_made"', self.profile_script)
        self.assertIn("custom league bonuses may differ", self.profile_script)
        self.assertNotIn("share the selected league scoring", self.profile_script)
        self.assertIn("const totals = new Map(keys.map(([key]) => [key, 0]))", self.profile_script)

    def test_partial_sleeper_counts_remain_visible_without_inventing_direction(self):
        self.assertIn("adds not published", self.profile_script.lower())
        self.assertIn("drops not published", self.profile_script.lower())
        self.assertIn("Direction stays unknown until both counts are observed", self.profile_script)
        self.assertIn('description.marketTrend?.direction || "unknown"', self.catalog_script)

    def test_selection_is_kept_on_the_rendered_catalog_page(self):
        self.assertIn("function selectionForPage(", self.catalog_script)
        self.assertIn("catalogUi.selectionForPage(", self.script)
        self.assertIn('row.setAttribute("aria-selected", String(selected))', self.catalog_script)
        previous = self.script[
            self.script.index('$("playerLabPreviousPage")?.addEventListener'):
            self.script.index('$("playerLabNextPage")?.addEventListener')
        ]
        next_page = self.script[
            self.script.index('$("playerLabNextPage")?.addEventListener'):
            self.script.index('$("playerLabPageSize")?.addEventListener')
        ]
        page_size = self.script[
            self.script.index('$("playerLabPageSize")?.addEventListener'):
            self.script.index('$("playerLabTableBody")?.addEventListener')
        ]
        for block in (previous, next_page, page_size):
            self.assertIn("render();", block)
        self.assertIn("void loadSelectedPlayerDetail()", self.script)

    def test_unknown_availability_preserves_retained_evidence_without_classifying(self):
        for phrase in (
            "Not classified · insufficient qualifying history",
            "practice_primary_injury",
            "practice_secondary_injury",
            "availability_contexts",
            "Documented non-injury contexts",
            "Game:",
            "Practice:",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.profile_script)
        unknown_branch = self.profile_script[
            self.profile_script.index('if (!history || history.status !== "observed")'):
            self.profile_script.index('const burdenTier = history.burden_tier')
        ]
        self.assertIn("appendAvailabilityEvidence(card, history)", unknown_branch)
        self.assertIn("appendAvailabilityContexts(card, history)", unknown_branch)
        self.assertNotIn("history unavailable", unknown_branch)

    def test_availability_ui_does_not_present_the_descriptive_index_as_probability(self):
        self.assertIn("weighted report index", self.profile_script)
        self.assertNotIn("/100 history score", self.profile_script)
        self.assertNotIn("Historical availability risk", self.profile_script)

    def test_catalog_rank_basis_and_source_size_copy_are_explicit(self):
        self.assertIn("function overallRankBasis(", self.script)
        self.assertIn("format.overallRankBasis(player)", self.catalog_script)
        rank_basis = self.script[
            self.script.index("function overallRankBasis("):
            self.script.index("function playerDescription(")
        ]
        self.assertNotIn("rest_of_season_ecr?.rank", rank_basis)
        self.assertIn('return "Local remaining projection"', rank_basis)
        self.assertIn("source response size at capture", self.script)
        self.assertNotIn("bytes retained", self.script)

    def test_source_debug_discloses_future_week_preview_scope(self):
        app_script = (
            PROJECT_ROOT / "trade_snapshot" / "web_assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("weekly_projection_preview", app_script)
        self.assertIn("collection stops at your league's regular-season endpoint", app_script)

    def test_player_lab_modules_stay_bounded_by_responsibility(self):
        self.assertLessEqual(len(self.script.splitlines()), 800)
        self.assertLessEqual(len(self.catalog_script.splitlines()), 800)
        self.assertLessEqual(len(self.profile_script.splitlines()), 600)


if __name__ == "__main__":
    unittest.main()
