from importlib.resources import files
import unittest


def asset(name: str) -> str:
    return files("trade_snapshot.web_assets").joinpath(name).read_text(encoding="utf-8")


class TradeTimingWebAssetTests(unittest.TestCase):
    def test_page_loads_separate_timing_assets_and_accessible_states(self):
        page = asset("index.html")

        self.assertIn('href="/trade_timing.css"', page)
        self.assertIn('src="/trade_timing_ui.js"', page)
        self.assertIn('id="tradeTimingLoading"', page)
        self.assertIn('id="tradeTimingError"', page)
        self.assertIn('id="tradeTimingPartnerBoard"', page)
        self.assertIn('id="tradeTimingLab" class="panel trade-timing-lab"', page)
        self.assertIn('id="tradeTimingLoadButton"', page)
        self.assertIn(
            'id="tradeTimingSummary" class="trade-timing-summary" role="status"',
            page,
        )
        self.assertIn('role="alert"', page)
        self.assertIn("Timing priority is not acceptance probability", page)

    def test_controller_cancels_stale_requests_and_preserves_evidence_boundaries(self):
        script = asset("trade_timing_ui.js")

        self.assertIn("window.TradeTimingUi", script)
        self.assertIn("AbortController", script)
        self.assertIn("controller.signal.aborted", script)
        self.assertIn("requestRevision", script)
        self.assertIn("primaryTeamId=", script)
        self.assertIn("setPrimaryTeam", script)
        self.assertIn("setPartnerTeam", script)
        self.assertIn("Historical completed-deal participation", script)
        self.assertIn("They are not market prices", script)
        self.assertIn("Verification required", script)
        self.assertIn("timing_partner_rank", script)
        self.assertIn('plan.trigger?.kind === "loss_and_downward_slope"', script)
        self.assertIn("Verified week-by-week history is unavailable", script)
        self.assertIn("replacement?.focus", script)
        self.assertIn("80% Wilson interval", script)
        self.assertIn("comparison sample gate is not met", script)
        self.assertIn("primary_display_power_delta", script)
        self.assertNotIn("Actionable now", script)
        self.assertIn('role: "img"', script)
        self.assertIn("renderPartnerBoard", script)
        self.assertNotIn("innerHTML", script)

    def test_app_synchronizes_bundle_and_primary_team(self):
        script = asset("app.js")

        self.assertIn("TradeTimingUi.setBundle", script)
        self.assertIn("apiRequest: api", script)
        self.assertIn("TradeTimingUi.setPrimaryTeam", script)
        self.assertIn('loadInsight("timing")', script)
        self.assertIn('$("tradeTimingLoadButton").addEventListener', script)

        gm_script = asset("gm_insights_ui.js")
        self.assertIn("TradeTimingUi?.setPartnerTeam", gm_script)

    def test_styles_include_responsive_and_reduced_motion_layouts(self):
        stylesheet = asset("trade_timing.css")

        self.assertIn(".trade-timing-lab", stylesheet)
        self.assertIn(".trade-timing-trajectory-figure", stylesheet)
        self.assertIn("prefers-reduced-motion", stylesheet)
        self.assertIn("@media (max-width: 680px)", stylesheet)


if __name__ == "__main__":
    unittest.main()
