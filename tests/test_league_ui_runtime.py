from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot.local_server import create_local_server


class LeagueUiRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright is optional for source-only test runs")
        cls._playwright_context = sync_playwright()
        cls._playwright = cls._playwright_context.start()
        cls._browser = None
        for options in (
            {"channel": "chromium", "headless": True},
            {"channel": "msedge", "headless": True},
            {"headless": True},
        ):
            try:
                cls._browser = cls._playwright.chromium.launch(**options)
                break
            except Exception:
                continue
        if cls._browser is None:
            cls._playwright.stop()
            raise unittest.SkipTest(
                "No Playwright-compatible Chromium or Edge browser is installed"
            )

    @classmethod
    def tearDownClass(cls):
        if cls._browser is not None:
            cls._browser.close()
        cls._playwright.stop()

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.server = create_local_server(self.directory.name)
        self.server.app_service.import_bundle(engine_bundle().to_record())
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.page = self._browser.new_page()
        self.page.set_default_timeout(5_000)
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def tearDown(self):
        self.page.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def add_league(self, name, espn_id, yahoo_id):
        self.page.locator("#addLeagueButton").click()
        self.page.locator("#leagueName").fill(name)
        self.page.locator("#collectionSeason").fill("2026")
        self.page.locator("#hostLeagueUrl").fill(
            f"https://fantasy.espn.com/football/league?leagueId={espn_id}"
        )
        self.page.locator("#yahooProjectionUrl").fill(
            f"https://football.fantasysports.yahoo.com/f1/{yahoo_id}/players"
        )
        self.page.locator("#saveLeagueButton").click()
        self.page.wait_for_function(
            "name => document.querySelector('#leagueSelect').selectedOptions[0]?.textContent === name",
            arg=f"{name} · 2026",
        )

    def test_multi_page_profile_list_keeps_unassigned_imports_visible(self):
        for number in range(205):
            self.server.app_service.create_league_profile({
                "name": f"League {number:03d}",
                "season": 2026,
                "scoring": "PPR",
                "host_league_url": (
                    "https://fantasy.espn.com/football/league?"
                    f"leagueId={10_000 + number}"
                ),
                "yahoo_projection_league_url": (
                    "https://football.fantasysports.yahoo.com/f1/201/players"
                ),
            })

        self.page.goto(
            self.server.app_url, wait_until="domcontentloaded", timeout=10_000
        )
        self.page.wait_for_function(
            "() => document.querySelector('#health')?.textContent === 'App running locally'",
            timeout=15_000,
        )
        self.page.wait_for_function(
            "() => document.querySelectorAll('#leagueSelect option').length === 207",
            timeout=10_000,
        )

        unassigned = self.page.locator('#leagueSelect option[value="unassigned"]')
        self.assertEqual(unassigned.count(), 1)
        self.assertEqual(unassigned.text_content(), "Unassigned imports · 1")
        self.assertEqual(self.page_errors, [])

    def test_multiple_leagues_assignment_saved_team_archive_and_dark_ui(self):
        self.page.goto(
            self.server.app_url, wait_until="domcontentloaded", timeout=10_000
        )
        self.page.wait_for_function(
            "() => document.querySelector('#health')?.textContent === 'App running locally'"
        )
        self.add_league("League One", "101", "201")
        self.add_league("League Two", "102", "202")

        league_select = self.page.locator("#leagueSelect")
        self.assertEqual(league_select.locator("option").count(), 4)
        league_select.select_option("unassigned")
        self.page.wait_for_function(
            "() => document.querySelector('#bundleSelect').value.startsWith('engine_')"
        )
        self.page.locator("#assignLeagueSelect").select_option(label="League One · 2026")
        self.page.locator("#assignBundleButton").click()
        self.page.wait_for_function(
            "() => document.querySelector('#leagueSelect').selectedOptions[0]?.textContent === 'League One · 2026'"
        )
        self.page.wait_for_function(
            "() => document.querySelector('#primaryTeam').options.length === 2"
        )

        self.page.locator("#primaryTeam").select_option("other")
        league_two_id = league_select.locator("option", has_text="League Two · 2026").get_attribute("value")
        catalog_request_started = Event()
        release_catalog_request = Event()
        original_catalog = self.server.app_service.league_bundle_catalog

        def delayed_catalog(profile_id):
            if profile_id == league_two_id:
                catalog_request_started.set()
                if not release_catalog_request.wait(5):
                    raise RuntimeError("test did not release the league catalog")
            return original_catalog(profile_id)

        self.server.app_service.league_bundle_catalog = delayed_catalog
        league_select.select_option(label="League Two · 2026")
        try:
            self.assertTrue(catalog_request_started.wait(5))
            self.assertTrue(self.page.locator("#bundleSelect").is_disabled())
            self.assertEqual(self.page.locator("#primaryTeam option").count(), 0)
            self.assertTrue(self.page.locator("#startButton").is_disabled())
        finally:
            release_catalog_request.set()
        self.page.wait_for_function(
            "() => document.querySelector('#bundleSelect').options.length === 1"
        )
        self.server.app_service.league_bundle_catalog = original_catalog
        league_select.select_option(label="League One · 2026")
        self.page.wait_for_function(
            "() => document.querySelector('#primaryTeam').value === 'other'"
        )

        self.page.locator("#skipSmall").uncheck()
        self.page.locator("#maxOutgoing").fill("1")
        self.page.locator("#maxIncoming").fill("1")
        self.page.locator("#maxTotal").fill("2")
        self.page.locator("#scenarioCount").fill("100")
        result_request_started = Event()
        release_result_request = Event()
        original_search_results = self.server.app_service.job_results

        def delayed_search_results(job_id):
            result_request_started.set()
            if not release_result_request.wait(5):
                raise RuntimeError("test did not release the result preview")
            return original_search_results(job_id)

        self.server.app_service.job_results = delayed_search_results
        self.page.locator("#editLeagueButton").click()
        self.page.locator("#startButton").click()
        try:
            self.assertTrue(result_request_started.wait(10))
            self.assertTrue(
                self.page.locator("#leagueEditor").evaluate(
                    "element => element.classList.contains('hidden')"
                )
            )
            for selector in (
                "#leagueSelect",
                "#bundleSelect",
                "#bundleFile",
                "#assignLeagueSelect",
                "#assignBundleButton",
                "#primaryTeam",
                "#counterparties",
                "#scenarioCount",
            ):
                with self.subTest(selector=selector):
                    self.assertTrue(self.page.locator(selector).is_disabled())
        finally:
            release_result_request.set()
        self.page.locator("#resultsPanel").wait_for(state="visible", timeout=10_000)

        self.assertTrue(self.page.locator("#onlyMutualResults").is_visible())
        self.assertTrue(self.page.locator("#minimumResultGain").is_visible())
        self.assertTrue(self.page.locator("#resultSort").is_visible())
        self.assertEqual(
            self.page.evaluate(
                "() => getComputedStyle(document.documentElement).backgroundColor"
            ),
            "rgb(7, 16, 20)",
        )

        self.page.locator("#archiveLeagueButton").click()
        self.page.wait_for_function(
            "() => document.querySelector('#leagueSelect').selectedOptions[0]?.textContent === 'League Two · 2026'"
        )
        self.page.locator("#showArchivedLeagues").check()
        self.page.wait_for_function(
            "() => [...document.querySelectorAll('#leagueSelect option')].some(option => option.textContent === 'League One · 2026 · archived')"
        )
        self.assertEqual(self.page_errors, [])


if __name__ == "__main__":
    unittest.main()
