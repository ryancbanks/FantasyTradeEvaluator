from pathlib import Path
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
                            "extension was not detected",
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
                        context.route(
                            "https://www.fantasypros.com/**",
                            lambda route: route.fulfill(
                                status=200,
                                content_type="text/html",
                                body="<!doctype html><title>Fixture analyzer</title><main></main>",
                            ),
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(server.app_url, wait_until="load")
                        page.wait_for_function(
                            "() => !document.querySelector('#connectExtensionButton').disabled"
                        )
                        page.click("#connectExtensionButton")
                        worker = self._service_worker(context)
                        self._wait_for(
                            lambda: worker.evaluate("statusSnapshot().phase")
                            == "pair_pending"
                        )
                        pair_hint = worker.evaluate("statusSnapshot().pair_hint")
                        page.wait_for_function(
                            "() => !document.querySelector('#extensionPairCode').classList.contains('hidden')"
                        )
                        self.assertIn(
                            pair_hint, page.locator("#extensionPairCode").inner_text()
                        )
                        worker.evaluate(
                            'handleMessage({kind: "fte.popup.reject"}, {})'
                        )
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
                        worker.evaluate("acceptPendingPair()")
                        self._wait_for(lambda: server.extension_bridge.state == "paired")

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
