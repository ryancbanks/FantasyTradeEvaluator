from pathlib import Path
import json
import os
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from trade_snapshot._extension_capture import ExtensionCaptureBackend
from trade_snapshot.browser_capture import BrowserCaptureOptions
from trade_snapshot.capture_schema import PageCaptureTask
from trade_snapshot.local_server import create_local_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = PROJECT_ROOT / "trade_snapshot" / "browser_extension"


class BrowserExtensionRuntimeTests(unittest.TestCase):
    def test_missing_extension_fails_promptly_and_allows_retry(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional for source-only tests")

        with TemporaryDirectory(dir=PROJECT_ROOT) as data, TemporaryDirectory(
            dir=PROJECT_ROOT
        ) as profile, patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"}):
            server = create_local_server(data)
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    try:
                        context = playwright.chromium.launch_persistent_context(
                            profile, channel="chromium", headless=True
                        )
                    except Exception:
                        self.skipTest("the local Chromium runtime is unavailable")
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(server.app_url, wait_until="load")
                        page.wait_for_function(
                            "() => !document.querySelector('#connectExtensionButton').disabled"
                        )
                        page.click("#connectExtensionButton")
                        page.wait_for_function(
                            "() => !document.querySelector('#errorBanner').classList.contains('hidden')",
                            timeout=6000,
                        )
                        self.assertIn(
                            "extension is not active on this page yet",
                            page.locator("#errorBanner").inner_text(),
                        )
                        self.assertTrue(page.locator("#connectExtensionButton").is_enabled())
                    finally:
                        context.close()
            finally:
                server.extension_bridge.close()
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()

    def test_real_manifest_pairs_and_owns_exactly_one_temporary_scan_tab(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional for source-only tests")

        with TemporaryDirectory(dir=PROJECT_ROOT) as data, TemporaryDirectory(
            dir=PROJECT_ROOT
        ) as profile, patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"}):
            server = create_local_server(data)
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    try:
                        context = playwright.chromium.launch_persistent_context(
                            profile,
                            channel="chromium",
                            headless=True,
                            args=[
                                f"--disable-extensions-except={EXTENSION_ROOT}",
                                f"--load-extension={EXTENSION_ROOT}",
                            ],
                        )
                    except Exception:
                        self.skipTest("the local Chromium runtime cannot load extensions")
                    try:
                        league_data = {
                            "season": 2026,
                            "league": {
                                "key": "runtime-only-private-key",
                                "season": 2026,
                                "settings": {
                                    "playoffsTeams": 1,
                                    "rosterSize": 1,
                                    "basic_scoring": "PPR",
                                },
                            },
                            "teams": [
                                {"id": 1, "teamName": "One", "players": [101]},
                                {"id": 2, "teamName": "Two", "players": [102]},
                            ],
                            "playerInfo": {
                                "101": {"player_id": 101, "player_name": "A"},
                                "102": {"player_id": 102, "player_name": "B"},
                            },
                        }
                        initial = {
                            "standings": [
                                {"teamId": 1, "wins": 1, "losses": 0, "ties": 0},
                                {"teamId": 2, "wins": 0, "losses": 1, "ties": 0},
                            ],
                            "best_free_agents": [{"id": 103}],
                        }
                        projected = {
                            "playoffsTeam": 1,
                            "standings": [
                                {
                                    "teamId": 1, "teamName": "One", "rank_proj": 1,
                                    "rank_current": 1, "wins_current": 1,
                                    "losses_current": 0, "wins_proj": 8,
                                    "losses_proj": 6, "playoffs_odds": 60,
                                    "championship_odds": 20,
                                },
                                {
                                    "teamId": 2, "teamName": "Two", "rank_proj": 2,
                                    "rank_current": 2, "wins_current": 0,
                                    "losses_current": 1, "wins_proj": 6,
                                    "losses_proj": 8, "playoffs_odds": 40,
                                    "championship_odds": 10,
                                },
                            ],
                        }
                        fixture = """<!doctype html><title>Fixture analyzer</title><main></main>
                        <script>
                        const data = Object.freeze(%s);
                        window.MPB = {getProjectedStandings: (_args, ok) => ok(%s)};
                        void fetch(
                          'https://mpbnfl.fantasypros.com/api/tradeAnalyzer' +
                          '?key=runtime-only-private-key&team1Id=1&period=ros&init=Y'
                        );
                        </script>""" % (json.dumps(league_data), json.dumps(projected))
                        context.route(
                            "https://www.fantasypros.com/**",
                            lambda route: route.fulfill(
                                status=200,
                                content_type="text/html",
                                body=fixture,
                            ),
                        )
                        context.route(
                            "https://mpbnfl.fantasypros.com/**",
                            lambda route: route.fulfill(
                                status=200,
                                headers={
                                    "Access-Control-Allow-Origin":
                                        "https://www.fantasypros.com",
                                    "Access-Control-Allow-Credentials": "true",
                                },
                                content_type="application/json",
                                body=json.dumps(initial),
                            ),
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(server.app_url, wait_until="load")
                        page.wait_for_function(
                            "() => !document.querySelector('#connectExtensionButton').disabled"
                        )
                        worker = self._service_worker(context)
                        popup = context.new_page()
                        popup.goto(
                            worker.evaluate(
                                "chrome.runtime.getURL('popup/popup.html')"
                            ),
                            wait_until="load",
                        )
                        popup.wait_for_function(
                            "() => document.querySelector('#status').textContent !== 'Checking…'"
                        )
                        self.assertFalse(popup.locator("#pair-actions").is_visible())
                        self.assertFalse(popup.locator("#connected-actions").is_visible())

                        page.click("#connectExtensionButton")
                        self._wait_for(
                            lambda: worker.evaluate("statusSnapshot().phase")
                            == "pair_pending"
                        )
                        popup.wait_for_function(
                            "() => !document.querySelector('#pair-actions').hidden"
                        )
                        self.assertTrue(popup.locator("#pair-actions").is_visible())
                        self.assertFalse(popup.locator("#connected-actions").is_visible())
                        pair_hint = worker.evaluate("statusSnapshot().pair_hint")
                        page.wait_for_function(
                            "() => !document.querySelector('#extensionPairCode').classList.contains('hidden')"
                        )
                        self.assertIn(
                            pair_hint, page.locator("#extensionPairCode").inner_text()
                        )
                        popup.click("#reject")
                        page.wait_for_function(
                            "() => !document.querySelector('#errorBanner').classList.contains('hidden')"
                        )
                        self.assertIn(
                            "did not accept",
                            page.locator("#errorBanner").inner_text(),
                        )
                        self.assertTrue(page.locator("#connectExtensionButton").is_enabled())

                        page.click("#connectExtensionButton")
                        self._wait_for(
                            lambda: worker.evaluate("statusSnapshot().phase")
                            == "pair_pending"
                        )
                        popup.wait_for_function(
                            "() => !document.querySelector('#pair-actions').hidden"
                        )
                        popup.click("#accept")
                        self._wait_for(lambda: server.extension_bridge.state == "paired")
                        popup.wait_for_function(
                            "() => !document.querySelector('#connected-actions').hidden"
                        )
                        self.assertFalse(popup.locator("#pair-actions").is_visible())
                        self.assertTrue(popup.locator("#connected-actions").is_visible())
                        popup.close()

                        session = ExtensionCaptureBackend(server.extension_bridge).open(
                            BrowserCaptureOptions(Path(data) / "unused", action_delay_ms=200),
                            5000,
                            lambda: False,
                        )
                        self._wait_for(
                            lambda: worker.evaluate("chrome.tabs.query({}).then(tabs => tabs.length)")
                            == 2
                        )
                        analyzer_url = (
                            "https://www.fantasypros.com/nfl/myplaybook/"
                            "trade-analyzer.php"
                        )
                        self._call_while_pumping(
                            page,
                            lambda: session.navigate(analyzer_url, 5000, lambda: False),
                        )
                        session.assert_page_provenance(
                            PageCaptureTask(
                                "fantasypros", 2026, 1, "league_source", analyzer_url
                            ),
                            analyzer_url,
                            5000,
                            lambda: False,
                        )
                        captured = []
                        self._call_while_pumping(
                            page,
                            lambda: captured.append(
                                session.capture_league_sources(
                                    PageCaptureTask(
                                        "fantasypros", 2026, 1, "league_source",
                                        analyzer_url,
                                    ),
                                    5000,
                                    lambda: False,
                                )
                            ),
                        )
                        self.assertEqual(captured[0].team_count, 2)
                        session.close(5000)
                        self._wait_for(
                            lambda: worker.evaluate("chrome.tabs.query({}).then(tabs => tabs.length)")
                            == 1
                        )
                        self._wait_for(
                            lambda: server.extension_bridge.state == "unpaired"
                        )
                        status = worker.evaluate("statusSnapshot()")
                        self.assertEqual(status["phase"], "idle")
                        self.assertIsNone(status["app_origin"])
                    finally:
                        context.close()
            finally:
                server.extension_bridge.close()
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()

    @staticmethod
    def _service_worker(context):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if context.service_workers:
                return context.service_workers[0]
            sleep(0.05)
        raise AssertionError("extension service worker did not start")

    @staticmethod
    def _wait_for(predicate):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if predicate():
                return
            sleep(0.05)
        raise AssertionError("extension runtime did not reach the expected state")

    @staticmethod
    def _call_while_pumping(page, operation):
        errors = []

        def invoke():
            try:
                operation()
            except BaseException as error:
                errors.append(error)

        worker = Thread(target=invoke, daemon=True)
        worker.start()
        deadline = monotonic() + 7
        while worker.is_alive() and monotonic() < deadline:
            page.wait_for_timeout(50)
        worker.join(timeout=0.1)
        if worker.is_alive():
            raise AssertionError("extension operation did not finish")
        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main()
