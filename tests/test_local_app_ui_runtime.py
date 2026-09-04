from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from tests.draft_fixtures import small_draft_config, small_historical_corpus
import tests.test_engine_bundle as engine_bundle_fixtures
import tests.test_search_runner as search_runner_fixtures
from tests.test_app_service import payload
from tests.test_engine_bundle import engine_bundle
from trade_snapshot.app_service import LocalSearchRequest
from trade_snapshot.draft_training import EvolutionConfig
from trade_snapshot.extension_bridge import PROTOCOL_VERSION, V1_CAPABILITIES
from trade_snapshot.local_server import create_local_server
from trade_snapshot.weekly_collection import (
    WeeklyCollectionError,
    WeeklyCollectionProgress,
    WeeklyCollectionRequest,
    WeeklyCollectionStage,
)


try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # The release CI installs the browser-test extra.
    PlaywrightError = RuntimeError
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright browser tests are not installed")
class LocalAppUiRuntimeTests(unittest.TestCase):
    def test_outdated_extension_is_blocked_with_reconnect_guidance(self):
        with TemporaryDirectory() as directory:
            server = create_local_server(directory)
            offer = server.extension_bridge.create_pairing()
            server.extension_bridge.connect(
                offer["pair_code"],
                protocol_version=PROTOCOL_VERSION,
                capabilities=V1_CAPABILITIES,
                extension_version="0.1.0",
            )
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    browser = _launch_browser(playwright)
                    try:
                        page = browser.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(server.app_url, wait_until="networkidle")
                        page.locator(
                            "#extensionStatus", has_text="update required"
                        ).wait_for()
                        self.assertTrue(page.locator("#collectButton").is_disabled())
                        self.assertFalse(
                            page.locator("#connectExtensionButton").is_disabled()
                        )
                        self.assertEqual(
                            page.locator("#connectExtensionButton").text_content(),
                            "Reconnect updated extension",
                        )
                        help_text = page.locator("#extensionHelp").text_content()
                        self.assertIn("Version 0.2.0 or newer", help_text)
                        self.assertIn("Reload", help_text)
                        self.assertEqual(page_errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()

    def test_trade_player_dashboard_and_draft_surfaces_work_together(self):
        with TemporaryDirectory() as directory:
            server = create_local_server(directory)
            # Give the fixture one equal-value bench exchange so the strict
            # five-point power gate has a valid row to render end to end.
            with patch.dict(engine_bundle_fixtures.ALL_POINTS, {"q2": 8.0}), \
                    patch.dict(search_runner_fixtures.PLAYER_POINTS, {"q2": 8.0}):
                bundle = engine_bundle()
            server.app_service.import_bundle(bundle.to_record())
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    browser = _launch_browser(playwright)
                    try:
                        page = browser.new_page()
                        page_errors = []
                        gm_requests = []
                        timing_requests = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))

                        def record_insight_request(request):
                            if request.url.endswith("/gm-insights"):
                                gm_requests.append(request.url)
                            if "/trade-timing?" in request.url:
                                timing_requests.append(request.url)

                        page.on("request", record_insight_request)
                        page.goto(server.app_url, wait_until="networkidle")
                        page.locator("#health", has_text="running").wait_for()

                        self.assertTrue(page.locator("#tradeLabPanel").is_visible())
                        self.assertFalse(page.locator("#draftLabPanel").is_visible())
                        self.assertFalse(page.locator("#dashboardContent").is_visible())
                        self.assertFalse(page.locator("#playerLabContent").is_visible())
                        self.assertFalse(page.locator("#gmInsightsContent").is_visible())
                        self.assertFalse(page.locator("#tradeTimingContent").is_visible())
                        self.assertEqual(gm_requests, [])
                        self.assertEqual(timing_requests, [])

                        page.locator("#playerLabLoadButton").click()
                        page.locator("#playerLabContent").wait_for(state="visible")
                        self.assertGreater(
                            page.locator("#playerLabTableBody tr").count(), 0
                        )

                        page.locator("#dashboardLoadButton").click()
                        page.locator("#dashboardContent").wait_for(
                            state="visible", timeout=15_000
                        )
                        self.assertGreater(
                            page.locator("#dashboardStandingsBody tr").count(), 0
                        )

                        page.locator("#gmInsightsLoadButton").click()
                        page.locator("#gmInsightsContent").wait_for(
                            state="visible", timeout=15_000
                        )
                        self.assertGreater(
                            page.locator("#gmInsightsTableBody tr").count(), 0
                        )
                        self.assertEqual(len(gm_requests), 1)
                        self.assertEqual(timing_requests, [])

                        page.locator("#tradeTimingLoadButton").click()
                        page.locator("#tradeTimingContent").wait_for(
                            state="visible", timeout=15_000
                        )
                        self.assertGreater(
                            page.locator("#tradeTimingPartnerBoard > *").count(), 0
                        )
                        self.assertEqual(len(timing_requests), 1)

                        page.locator("#maxOutgoing").fill("1")
                        page.locator("#maxIncoming").fill("1")
                        page.locator("#maxTotal").fill("2")
                        page.locator("#scenarioCount").fill(
                            str(max(100, bundle.scenario_config.scenario_count))
                        )
                        page.locator("#skipSmall").uncheck()
                        page.select_option("#counterparties", ["other"])
                        self.assertFalse(
                            page.locator("#startButton").is_disabled(),
                            page.evaluate(
                                "() => ({bundle: document.querySelector('#bundleSelect').value, "
                                "format: document.querySelector('input[name=tradeFormat]:checked').value, "
                                "estimate: document.querySelector('#estimate').textContent})"
                            ),
                        )
                        invalid = page.eval_on_selector_all(
                            "#searchForm :invalid",
                            "elements => elements.map(element => "
                            "({id: element.id, message: element.validationMessage, "
                            "html: element.outerHTML}))",
                        )
                        self.assertEqual(invalid, [])
                        page.locator("#startButton").click()
                        try:
                            page.locator("#resultsPanel").wait_for(
                                state="visible", timeout=15_000
                            )
                        except PlaywrightError as error:
                            self.fail(
                                f"search did not finish: {error}; "
                                f"banner={page.locator('#errorBanner').text_content()!r}; "
                                f"progress={page.locator('#progressText').text_content()!r}; "
                                f"page_errors={page_errors!r}; "
                                f"activity={server.app_service.active_job_catalog()!r}"
                            )
                        self.assertGreater(
                            page.locator("#resultsBody tr").count(), 0
                        )

                        page.locator("#draftLabTab").click()
                        page.locator("#draftLabPanel").wait_for(state="visible")
                        page.locator("#draftYearNotice", has_text="2015").wait_for()
                        page.locator("#tradeLabTab").click()
                        self.assertTrue(page.locator("#tradeLabPanel").is_visible())
                        self.assertEqual(page_errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()

    def test_reload_recovers_and_can_cancel_active_collection(self):
        workflow_started = Event()
        workflow_stopped = Event()

        def blocking_workflow(
            request,
            *,
            data_directory,
            progress,
            cancelled,
        ):
            workflow_started.set()
            progress(
                WeeklyCollectionProgress(
                    WeeklyCollectionStage.COLLECTING_LEAGUE,
                    0.25,
                    "Scanning league rosters",
                )
            )
            while not cancelled():
                workflow_stopped.wait(0.01)
            workflow_stopped.set()
            raise WeeklyCollectionError("Weekly collection was cancelled")

        with TemporaryDirectory() as directory:
            server = create_local_server(
                directory,
                weekly_collection_workflow=blocking_workflow,
            )
            job = server.app_service.start_weekly_collection(
                WeeklyCollectionRequest(
                    season=2026,
                    week=1,
                    scoring="PPR",
                    include_future_weekly=False,
                )
            )
            self.assertTrue(workflow_started.wait(2))
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    browser = _launch_browser(playwright)
                    try:
                        page = browser.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(server.app_url, wait_until="networkidle")
                        page.locator("#cancelCollectionButton").wait_for(
                            state="visible", timeout=5_000
                        )
                        self.assertIn(
                            "Scanning league rosters",
                            page.locator("#collectionProgressText").text_content(),
                        )

                        page.reload(wait_until="networkidle")
                        page.locator("#cancelCollectionButton").wait_for(
                            state="visible", timeout=5_000
                        )
                        self.assertIn(
                            "Scanning league rosters",
                            page.locator("#collectionProgressText").text_content(),
                        )

                        page.locator("#cancelCollectionButton").click()
                        page.locator("#cancelCollectionButton").wait_for(
                            state="hidden", timeout=5_000
                        )
                        page.locator(
                            "#collectionProgressText", has_text="stopped safely"
                        ).wait_for(
                            timeout=5_000
                        )
                        self.assertTrue(workflow_stopped.wait(2))
                        self.assertEqual(
                            server.app_service.weekly_collection(job["job_id"])["status"],
                            "cancelled",
                        )
                        _wait_until(
                            lambda: server.app_service.active_job_catalog()[
                                "weekly_collection"
                            ]
                            is None,
                            timeout=5,
                        )
                        self.assertEqual(page_errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()

    def test_reload_recovers_one_failed_collection_message(self):
        def failed_workflow(
            request,
            *,
            data_directory,
            progress,
            cancelled,
        ):
            raise WeeklyCollectionError("Projection provider was unavailable")

        with TemporaryDirectory() as directory:
            server = create_local_server(
                directory,
                weekly_collection_workflow=failed_workflow,
            )
            job = server.app_service.start_weekly_collection(
                WeeklyCollectionRequest(
                    season=2026,
                    week=1,
                    scoring="PPR",
                    include_future_weekly=False,
                )
            )
            _wait_for_terminal(
                lambda: server.app_service.weekly_collection(job["job_id"]),
                timeout=5,
            )
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    browser = _launch_browser(playwright)
                    try:
                        page = browser.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(server.app_url, wait_until="networkidle")
                        page.locator(
                            "#collectionProgressText", has_text="Collection failed"
                        ).wait_for(timeout=5_000)
                        self.assertIn(
                            "Projection provider was unavailable",
                            page.locator("#errorBanner").text_content(),
                        )
                        _wait_until(
                            lambda: server.app_service.active_job_catalog()[
                                "weekly_collection"
                            ]
                            is None,
                            timeout=5,
                        )
                        page.locator("#draftLabTab").click()
                        page.locator("#draftYearNotice", has_text="2015").wait_for()
                        self.assertFalse(
                            page.locator("#draftEstimateButton").is_disabled()
                        )

                        page.reload(wait_until="networkidle")
                        self.assertFalse(page.locator("#errorBanner").is_visible())
                        self.assertEqual(page_errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()

    def test_reload_recovers_one_completed_search_with_its_visible_team_scope(self):
        with TemporaryDirectory() as directory:
            server = create_local_server(directory)
            bundle = engine_bundle()
            server.app_service.import_bundle(bundle.to_record())
            request_record = payload(bundle.bundle_id)
            request_record["counterparty_team_ids"] = ["other"]
            job = server.app_service.start_search(
                LocalSearchRequest.from_payload(request_record)
            )
            _wait_for_terminal(
                lambda: server.app_service.job(job["job_id"]),
                timeout=5,
            )
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    browser = _launch_browser(playwright)
                    try:
                        page = browser.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(server.app_url, wait_until="networkidle")
                        page.locator("#resultsPanel").wait_for(
                            state="visible", timeout=10_000
                        )
                        self.assertEqual(
                            page.locator("#bundleSelect").input_value(),
                            bundle.bundle_id,
                        )
                        self.assertEqual(
                            page.locator("#primaryTeam").input_value(), "primary"
                        )
                        self.assertTrue(page.locator("#twoTeamFormat").is_checked())
                        self.assertEqual(
                            page.eval_on_selector_all(
                                "#counterparties option:checked",
                                "options => options.map(option => option.value)",
                            ),
                            ["other"],
                        )
                        self.assertIn(
                            "not reconstructed",
                            page.locator("#estimate").text_content(),
                        )
                        self.assertTrue(page.locator("#startButton").is_disabled())
                        self.assertGreater(page.locator("#resultsBody tr").count(), 0)
                        _wait_until(
                            lambda: server.app_service.active_job_catalog()["search"]
                            is None,
                            timeout=5,
                        )

                        page.reload(wait_until="networkidle")
                        self.assertFalse(page.locator("#resultsPanel").is_visible())

                        page.evaluate(
                            """() => {
                              bundles[0].teams.push({team_id: "third", name: "Third", players: []});
                              renderBundle();
                              restoreActiveWork({
                                weekly_collection: null,
                                draft: null,
                                search: {
                                  job_id: "ffffffffffffffffffffffffffffffff",
                                  status: "cancelled",
                                  trade_format: "three_team",
                                  request: {
                                    bundle_id: bundles[0].bundle_id,
                                    primary_team_id: "primary",
                                    counterparty_team_ids: ["other", "third"]
                                  },
                                  progress: null,
                                  error: null
                                }
                              });
                            }"""
                        )
                        self.assertTrue(page.locator("#threeTeamFormat").is_checked())
                        self.assertEqual(
                            {
                                page.locator("#partnerTeamA").input_value(),
                                page.locator("#partnerTeamB").input_value(),
                            },
                            {"other", "third"},
                        )
                        self.assertEqual(page_errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()

    def test_reload_recovers_one_completed_draft_training_result(self):
        with TemporaryDirectory() as directory:
            server = create_local_server(directory)
            corpus = small_historical_corpus()
            config = small_draft_config()
            server.app_service.draft_lab.import_corpus(corpus.to_record())
            job = server.app_service.draft_lab.start_training(
                {
                    "corpus_id": corpus.corpus_id,
                    "league_config": config.to_record(),
                    "evolution_config": EvolutionConfig(
                        4, 1, 1, 0.25, 0.1, 1_000, 4, (2025,), 2
                    ).to_record(),
                }
            )
            _wait_for_terminal(
                lambda: server.app_service.draft_lab.job(job["job_id"]),
                timeout=10,
            )
            serving = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            serving.start()
            try:
                with sync_playwright() as playwright:
                    browser = _launch_browser(playwright)
                    try:
                        page = browser.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(server.app_url, wait_until="networkidle")
                        page.locator("#draftLabTab").click()
                        page.locator("#draftLastBatch").wait_for(
                            state="visible", timeout=10_000
                        )
                        self.assertIn(
                            "Training complete",
                            page.locator("#draftProgressText").text_content(),
                        )
                        _wait_until(
                            lambda: server.app_service.active_job_catalog()["draft"]
                            is None,
                            timeout=5,
                        )

                        page.reload(wait_until="networkidle")
                        page.locator("#draftLabTab").click()
                        self.assertFalse(page.locator("#draftLastBatch").is_visible())
                        self.assertEqual(page_errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                serving.join(timeout=2)
                server.server_close()


def _launch_browser(playwright):
    errors = []
    for options in (
        {"channel": "chrome", "headless": True},
        {"channel": "msedge", "headless": True},
        {"headless": True},
    ):
        try:
            return playwright.chromium.launch(**options)
        except PlaywrightError as error:
            errors.append(str(error))
    raise unittest.SkipTest(
        "No Playwright Chromium, Chrome, or Edge runtime is available: "
        + " | ".join(errors)
    )


def _wait_for_terminal(read_job, *, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = read_job()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("background job did not finish before the timeout")


def _wait_until(predicate, *, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before the timeout")


if __name__ == "__main__":
    unittest.main()
