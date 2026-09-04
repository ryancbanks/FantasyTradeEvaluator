import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from trade_snapshot.espn_projection_read import espn_season_projection_segment
from tests.test_espn_projection_read import payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    PROJECT_ROOT
    / "trade_snapshot"
    / "browser_extension"
    / "collectors"
    / "espn_projection_main.js"
).read_text(encoding="utf-8")
PAGE = "https://fantasy.espn.com/football/players/projections#fte-scan-v1"


def launch_test_browser(playwright, test_case):
    for options in (
        {"channel": "chromium", "headless": True},
        {"channel": "msedge", "headless": True},
        {"headless": True},
    ):
        try:
            return playwright.chromium.launch(**options)
        except Exception:
            continue
    test_case.skipTest("No Playwright-compatible Chromium or Edge browser is installed")


class EspnProjectionExtensionScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright is optional for source-only test runs")
        cls._playwright_context = sync_playwright()
        cls._playwright = cls._playwright_context.start()
        cls._browser = launch_test_browser(cls._playwright, cls)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_browser"):
            cls._browser.close()
        if hasattr(cls, "_playwright"):
            cls._playwright.stop()

    def test_one_public_fetch_matches_the_python_sanitized_table(self):
        calls = []
        page = self.page()

        def api(route):
            calls.append(route.request)
            route.fulfill(
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "https://fantasy.espn.com"},
                body=json.dumps(payload(), separators=(",", ":")),
            )

        page.route("https://lm-api-reads.fantasy.espn.com/**", api)
        result = page.evaluate(
            """async (request) =>
              await globalThis.__FTE_MAIN_HANDLERS['espn.season_projections'](request)
            """,
            request(),
        )
        expected = espn_season_projection_segment(
            payload(), season=2026, scoring="HALF", league_format_id=8
        )
        self.assertEqual(result, expected)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(urlsplit(call.url).path, (
            "/apis/v3/games/ffl/seasons/2026/segments/0/leaguedefaults/8"
        ))
        self.assertEqual(urlsplit(call.url).query, "view=kona_player_info")
        headers = call.headers
        self.assertEqual(headers["x-fantasy-platform"], "espn-fantasy-web")
        self.assertEqual(headers["x-fantasy-source"], "kona")
        filter_value = json.loads(headers["x-fantasy-filter"])["players"]
        self.assertEqual(filter_value["limit"], 5000)
        self.assertEqual(filter_value["filterStatsForExternalIds"]["value"], [2026])
        self.assertEqual(filter_value["filterStatsForSourceIds"]["value"], [1])
        self.assertEqual(filter_value["sortAppliedStatTotal"]["value"], "102026")
        self.assertNotIn("filterStatsForTopScoringPeriodIds", filter_value)
        self.assertNotIn("cookie", headers)

    def test_wrong_season_and_weekly_request_fail_closed(self):
        page = self.page()
        page.route(
            "https://lm-api-reads.fantasy.espn.com/**",
            lambda route: route.fulfill(
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "https://fantasy.espn.com"},
                body=json.dumps(payload()),
            ),
        )
        weekly = request()
        weekly["horizon"] = "weekly"
        with self.assertRaisesRegex(Exception, "espn_projection_request"):
            page.evaluate(
                """async (request) =>
                  await globalThis.__FTE_MAIN_HANDLERS['espn.season_projections'](request)
                """,
                weekly,
            )
        wrong = payload()
        for wrapper in wrong["players"]:
            for stat in wrapper["player"]["stats"]:
                stat["seasonId"] = 2025
        page.unroute("https://lm-api-reads.fantasy.espn.com/**")
        page.route(
            "https://lm-api-reads.fantasy.espn.com/**",
            lambda route: route.fulfill(
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "https://fantasy.espn.com"},
                body=json.dumps(wrong),
            ),
        )
        with self.assertRaisesRegex(Exception, "espn_projection_empty"):
            page.evaluate(
                """async (value) =>
                  await globalThis.__FTE_MAIN_HANDLERS['espn.season_projections'](value)
                """,
                request(),
            )

    def test_extension_routes_ros_capture_to_the_season_collector(self):
        worker = (PROJECT_ROOT / "trade_snapshot" / "browser_extension" / "service_worker.js").read_text(
            encoding="utf-8"
        )
        manifest = json.loads(
            (PROJECT_ROOT / "trade_snapshot" / "browser_extension" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        dispatcher = (
            PROJECT_ROOT / "trade_snapshot" / "browser_extension" / "main_dispatcher.js"
        ).read_text(encoding="utf-8")
        agent = (PROJECT_ROOT / "trade_snapshot" / "browser_extension" / "scan_agent.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('payload.request.provider === "espn"', worker)
        self.assertIn('payload.request.horizon === "ros"', worker)
        self.assertIn('"espn.season_projections"', worker)
        self.assertIn('"espn.season_projections"', dispatcher)
        self.assertIn('"espn.season_projections"', agent)
        main_scripts = next(
            entry["js"] for entry in manifest["content_scripts"]
            if "main_dispatcher.js" in entry["js"]
        )
        self.assertLess(
            main_scripts.index("collectors/espn_projection_main.js"),
            main_scripts.index("main_dispatcher.js"),
        )

    def page(self):
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.route(
            "https://fantasy.espn.com/**",
            lambda route: route.fulfill(content_type="text/html", body="<main></main>"),
        )
        page.goto(PAGE)
        page.evaluate("globalThis.__FTE_MAIN_HANDLERS = {}")
        page.evaluate(SCRIPT)
        return page


def request():
    return {
        "provider": "espn",
        "season": 2026,
        "week": 2,
        "horizon": "ros",
        "scoring": "HALF",
        "positions": ["ALL"],
        "timeout_ms": 5000,
    }


if __name__ == "__main__":
    unittest.main()
