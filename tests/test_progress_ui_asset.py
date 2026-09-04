import unittest
from pathlib import Path

ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "trade_snapshot"
    / "web_assets"
    / "progress_ui.js"
)
ASSET_SOURCE = ASSET_PATH.read_text(encoding="utf-8")
INDEX_SOURCE = (ASSET_PATH.parent / "index.html").read_text(encoding="utf-8")


class ProgressUiStaticContractTests(unittest.TestCase):
    def test_progress_bars_are_named_without_chatty_live_wrappers(self):
        for label in (
            "Current operation progress",
            "Weekly data collection progress",
            "Trade search progress",
        ):
            with self.subTest(label=label):
                self.assertIn(f'aria-label="{label}"', INDEX_SOURCE)
        self.assertNotIn(
            'id="operationProgress" class="operation-progress hidden" aria-live=',
            INDEX_SOURCE,
        )
        self.assertNotIn(
            'id="collectionProgress" class="collection-progress hidden" aria-live=',
            INDEX_SOURCE,
        )

    def test_unknown_work_stays_indeterminate_and_timing_evidence_is_disclosed(self):
        self.assertIn("window.ProgressUi", ASSET_SOURCE)
        self.assertIn("based on previous app runs in this browser", ASSET_SOURCE)
        self.assertIn("confidence, measured this run", ASSET_SOURCE)
        self.assertIn("setBar(track, bar, null", ASSET_SOURCE)
        self.assertNotIn("elapsedSeconds(clock) / expected", ASSET_SOURCE)
        for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval("):
            with self.subTest(unsafe_sink=unsafe_sink):
                self.assertNotIn(unsafe_sink, ASSET_SOURCE)


class ProgressUiRuntimeTests(unittest.TestCase):
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

    def test_bar_semantics_and_observed_eta_description(self):
        page = self._browser.new_page()
        try:
            page.set_content(
                '<div id="track"><div id="bar"></div></div>'
                '<section id="operationProgress" class="hidden">'
                '<div id="operationProgressTrack">'
                '<div id="operationProgressBar"></div></div>'
                '<p id="operationProgressText"></p></section>'
            )
            page.add_script_tag(content=ASSET_SOURCE)
            result = page.evaluate(
                """() => {
                  const track = document.getElementById("track");
                  const bar = document.getElementById("bar");
                  ProgressUi.setBar(track, bar, null, "Preparing locally");
                  const unknown = {
                    indeterminate: track.classList.contains("indeterminate"),
                    now: track.getAttribute("aria-valuenow"),
                    text: track.getAttribute("aria-valuetext")
                  };
                  ProgressUi.setBar(track, bar, 0.625, "625 of 1000");
                  return {
                    unknown,
                    exact: {
                      indeterminate: track.classList.contains("indeterminate"),
                      now: track.getAttribute("aria-valuenow"),
                      width: bar.style.width
                    },
                    timing: ProgressUi.describeTiming({
                      activity: "active", status: "running", elapsed_seconds: 12,
                      eta: {low_seconds: 20, likely_seconds: 30, high_seconds: 40,
                            confidence: "medium", basis: "observed_phase_throughput"}
                    })
                  };
                }"""
            )
        finally:
            page.close()

        self.assertEqual(
            result["unknown"],
            {"indeterminate": True, "now": None, "text": "Preparing locally"},
        )
        self.assertEqual(
            result["exact"],
            {"indeterminate": False, "now": "62.5", "width": "62.5%"},
        )
        self.assertIn("20 sec–40 sec left", result["timing"])
        self.assertIn("medium confidence, measured this run", result["timing"])

    def test_terminal_timing_never_claims_time_is_still_remaining(self):
        page = self._browser.new_page()
        try:
            page.set_content(
                '<section id="operationProgress" class="hidden">'
                '<div id="operationProgressTrack">'
                '<div id="operationProgressBar"></div></div>'
                '<p id="operationProgressText"></p></section>'
            )
            page.add_script_tag(content=ASSET_SOURCE)
            result = page.evaluate(
                """() => ({
                  complete: ProgressUi.describeTiming({
                    activity: "terminal", status: "complete", elapsed_seconds: 12,
                    eta: null
                  }),
                  failed: ProgressUi.describeTiming({
                    activity: "terminal", status: "failed", elapsed_seconds: 8,
                    eta: null
                  })
                })"""
            )
        finally:
            page.close()

        self.assertEqual(result["complete"], "Complete · 12 sec active")
        self.assertEqual(result["failed"], "Failed · 8 sec active")
        self.assertNotIn("left", result["complete"])

    def test_an_older_concurrent_run_cannot_overwrite_or_unlock_the_latest(self):
        page = self._browser.new_page()
        try:
            page.set_content(
                '<main><section id="operationProgress" class="hidden">'
                '<div id="operationProgressTrack">'
                '<div id="operationProgressBar"></div></div>'
                '<p id="operationProgressText"></p></section>'
                '<section id="workspace"></section></main>'
            )
            page.add_script_tag(content=ASSET_SOURCE)
            result = page.evaluate(
                """async () => {
                  let finishFirst;
                  let finishSecond;
                  const first = ProgressUi.run("first", "First operation", () =>
                    new Promise(resolve => { finishFirst = resolve; })
                  );
                  const second = ProgressUi.run("second", "Second operation", () =>
                    new Promise(resolve => { finishSecond = resolve; })
                  );
                  const before = document.querySelector('#operationProgressText').textContent;
                  finishFirst("first");
                  await first;
                  const afterFirst = {
                    text: document.querySelector('#operationProgressText').textContent,
                    busy: document.querySelector('main').getAttribute('aria-busy'),
                    inert: document.querySelector('#workspace').inert
                  };
                  finishSecond("second");
                  await second;
                  return {
                    before,
                    afterFirst,
                    afterSecond: {
                      text: document.querySelector('#operationProgressText').textContent,
                      busy: document.querySelector('main').getAttribute('aria-busy'),
                      inert: document.querySelector('#workspace').inert
                    }
                  };
                }"""
            )
        finally:
            page.close()

        self.assertIn("Second operation", result["before"])
        self.assertIn("Second operation", result["afterFirst"]["text"])
        self.assertEqual(result["afterFirst"]["busy"], "true")
        self.assertTrue(result["afterFirst"]["inert"])
        self.assertIn("Second operation complete", result["afterSecond"]["text"])
        self.assertIsNone(result["afterSecond"]["busy"])
        self.assertFalse(result["afterSecond"]["inert"])

    def test_history_survives_port_changes_and_ignores_malformed_cookie_rows(self):
        context = self._browser.new_context()
        first = context.new_page()
        second = context.new_page()
        html = (
            '<section id="operationProgress" class="hidden">'
            '<div id="operationProgressTrack">'
            '<div id="operationProgressBar"></div></div>'
            '<p id="operationProgressText"></p></section>'
        )

        def serve_page(route):
            route.fulfill(status=200, content_type="text/html", body=html)

        try:
            for page in (first, second):
                page.route("**/*", serve_page)
            first.goto("http://127.0.0.1:48191/")
            first.evaluate(
                """() => {
                  const key = encodeURIComponent(
                    "fantasy-trade-evaluator.operation-history.v1"
                  );
                  const value = encodeURIComponent(JSON.stringify({bad: null}));
                  document.cookie = `${key}=${value}; Path=/; SameSite=Strict`;
                }"""
            )
            first.add_script_tag(content=ASSET_SOURCE)
            stored = first.evaluate(
                """() => {
                  const clock = ProgressUi.startHistory("bundle-load");
                  clock.started_at -= 2000;
                  ProgressUi.finishHistory(clock, true);
                  return JSON.parse(ProgressUi.readDeviceValue(
                    "fantasy-trade-evaluator.operation-history.v1"
                  ));
                }"""
            )

            second.goto("http://127.0.0.1:48192/")
            second.add_script_tag(content=ASSET_SOURCE)
            timing = second.evaluate(
                """() => ProgressUi.describeTiming(
                  null, ProgressUi.startHistory("bundle-load")
                )"""
            )
        finally:
            context.close()

        self.assertNotIn("bad", stored)
        self.assertIn("bundle-load", stored)
        self.assertIn("previous app runs in this browser", timing)


if __name__ == "__main__":
    unittest.main()
