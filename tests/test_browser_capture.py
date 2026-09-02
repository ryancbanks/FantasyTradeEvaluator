import json
import pickle
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from tests.capture_fixtures import league_capture_value, league_sources
from trade_snapshot.analyzer_contract import CURRENT_BUNDLE_FINGERPRINT

from trade_snapshot._capture_scripts import (
    ADVANCE_PROJECTION_SCRIPT, ANALYZER_BUNDLE_SOURCE_SCRIPT,
    ANALYZER_TAP_SCRIPT,
    CONFIGURE_PROJECTION_SCRIPT,
    ECR_TABLE_SCRIPT,
    FULL_ANALYSIS_ACTION_SCRIPT,
    LEAGUE_SOURCE_SCRIPT,
    PAGE_PROVENANCE_SCRIPT,
    PROJECTION_TABLE_SCRIPT,
    SINGLE_PAGE_SCRIPT,
    TAKE_ANALYZER_BODY_SCRIPT,
    YAHOO_SCORING_SCRIPT,
)
from trade_snapshot._capture_task_policy import yahoo_settings_url
from trade_snapshot._ecr_page import ecr_capture_data
from trade_snapshot._playwright_capture import (
    PlaywrightCaptureBackend,
    _PlaywrightSession,
)
from trade_snapshot._playwright_worker import (
    _WorkerSession,
    _decode_result,
    _encode_result,
)
from trade_snapshot._projection_tables import projection_capture
from trade_snapshot.browser_capture import (
    BrowserCaptureCancelled,
    BrowserCaptureDependencyError,
    BrowserCaptureError,
    BrowserCaptureOptions,
    BrowserCaptureTimeout,
    BrowserCollector,
    ECRCaptureData,
    LeagueCaptureData,
    ProjectionCaptureData,
    YahooScoringError,
)
from trade_snapshot.capture_schema import (
    AnalyzerCapturePhase,
    AnalyzerTradeSpec,
    CapturePlan,
    CaptureProvider,
    ECRRankingRow,
    FantasyProsECRTask,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    LeagueSource,
    LeagueSourceKind,
    PageCaptureTask,
    ProjectionTableSpec,
    VisibleTable,
    VisibleTableCell,
)


CAPTURED = datetime(2026, 9, 1, 14, 15, 16, tzinfo=timezone.utc)
PLAYOFF_BODY = {"playoffs": {
    "oddsBefore_team1": 20.0, "oddsAfter_team1": 30.0,
    "oddsBefore_team2": 40.0, "oddsAfter_team2": 35.0,
}}
POWER_ROWS = [{"teamId": 1, "score_decimal": 100.0}, {"teamId": 2, "score_decimal": 98.0}]
POWER_BODY = {"ros": {"powerRankings": {"before": POWER_ROWS, "after": list(reversed(POWER_ROWS))}}}


class BrowserCollectorTests(unittest.TestCase):
    def test_sequential_run_constructs_trade_query_clicks_full_and_returns_all_sources(self):
        clock = FakeClock()
        session = FakeSession(clock)
        backend = FakeBackend(session)
        plan = make_plan()
        espn = next(task for task in plan.tasks if task.provider is CaptureProvider.ESPN)
        artifacts = BrowserCollector(backend, clock=clock, now=lambda: CAPTURED).collect(
            plan, BrowserCaptureOptions(Path("profile"), action_delay_ms=200),
            sign_in_gate=FakeGate(),
            navigation_bindings={
                espn.task_id: espn.url + "?leagueId=123&seasonId=2026"
            },
        )

        self.assertEqual(backend.open_count, 1)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(len(artifacts), len(plan.tasks))
        analyzer_url = next(value for name, value, _ in session.operations if name == "navigate")
        self.assertIn("team2Id=2", analyzer_url)
        self.assertIn("team1Gets=1001%2C1002", analyzer_url)
        self.assertIn("team2Gets=2001%2C2002", analyzer_url)
        self.assertTrue(any(name == "full" for name, _, _ in session.operations))
        self.assertEqual(
            {artifact.provider for artifact in artifacts if isinstance(artifact, GenericTableArtifact)},
            {CaptureProvider.FANTASYPROS, CaptureProvider.ESPN, CaptureProvider.YAHOO},
        )
        self.assertEqual(
            sum(isinstance(artifact, FantasyProsLeagueArtifact) for artifact in artifacts), 1
        )
        self.assertTrue(all(
            later - earlier >= 0.2 - 1e-9
            for earlier, later in zip(session.action_times, session.action_times[1:])
        ))
        self.assertNotIn("leagueId", str([artifact.to_record() for artifact in artifacts]))

    def test_runtime_navigation_is_ephemeral_same_path_and_secret_free(self):
        task = projection_task("espn")
        plan = CapturePlan((task,))
        collector = BrowserCollector(FakeBackend(FakeSession(FakeClock())))
        for url in (
            "https://evil.example/football/players?leagueId=1",
            task.url + "/other?leagueId=1",
            task.url + "?access_token=secret",
            task.url + "#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                collector.collect(
                    plan, BrowserCaptureOptions(Path("profile")),
                    navigation_bindings={task.task_id: url},
                )
        with self.assertRaises(ValueError):
            collector.collect(
                CapturePlan((analyzer_task(),)), BrowserCaptureOptions(Path("profile")),
                navigation_bindings={analyzer_task().task_id: analyzer_task().url + "?x=1"},
            )
        yahoo = projection_task("yahoo")
        for page in ("players", "playersearch"):
            artifacts = collector.collect(
                CapturePlan((yahoo,)), BrowserCaptureOptions(Path("profile")),
                navigation_bindings={
                    yahoo.task_id: (
                        "https://football.fantasysports.yahoo.com/2026/f1/12345/"
                        f"{page}?status=ALL"
                    )
                },
            )
            self.assertEqual(len(artifacts), 1)
            self.assertNotIn("12345", str(artifacts[0].to_record()))

    def test_yahoo_final_page_stays_bound_to_same_league_and_selected_season(self):
        task = projection_task("yahoo")
        planned = (
            "https://football.fantasysports.yahoo.com/f1/12345/players?status=ALL"
        )
        accepted = (
            "https://football.fantasysports.yahoo.com/f1/12345/playersearch",
            "https://football.fantasysports.yahoo.com/2026/f1/12345/players",
        )
        for actual in accepted:
            page = FakePage()
            page.url = actual
            session = _PlaywrightSession(
                FakeContext((page,)), FakeManager(), FakeTimeoutError
            )
            session.assert_page_provenance(task, planned, 100, lambda: False)
            session.close()
        rejected = (
            "https://football.fantasysports.yahoo.com/f1/99999/players",
            "https://football.fantasysports.yahoo.com/2025/f1/12345/players",
        )
        for actual in rejected:
            page = FakePage()
            page.url = actual
            session = _PlaywrightSession(
                FakeContext((page,)), FakeManager(), FakeTimeoutError
            )
            with self.assertRaisesRegex(BrowserCaptureError, "redirected"):
                session.assert_page_provenance(task, planned, 100, lambda: False)
            session.close()

    def test_cancellation_overall_timeout_and_api_path_fail_before_partial_result(self):
        event = threading.Event()
        event.set()
        backend = FakeBackend(FakeSession(FakeClock()))
        with self.assertRaises(BrowserCaptureCancelled):
            BrowserCollector(backend).collect(
                make_plan(), BrowserCaptureOptions(Path("profile")), cancellation=event,
            )
        self.assertEqual(backend.open_count, 0)

        clock = FakeClock()
        slow = FakeSession(clock, navigation_duration=1)
        with self.assertRaises(BrowserCaptureTimeout):
            BrowserCollector(FakeBackend(slow), clock=clock).collect(
                CapturePlan((projection_task("espn"),)),
                BrowserCaptureOptions(Path("profile"), overall_timeout_ms=100),
            )
        self.assertEqual(slow.close_count, 1)

        api = FantasyProsECRTask(
            2026, 1, "weekly", "PPR", ("RB",), (), None,
            "https://api.fantasypros.com/v2/nfl/rankings", "official_api",
        )
        api_backend = FakeBackend(FakeSession(FakeClock()))
        with self.assertRaisesRegex(BrowserCaptureError, "caller-supplied-key"):
            BrowserCollector(api_backend).collect(
                CapturePlan((api,)), BrowserCaptureOptions(Path("profile"))
            )
        self.assertEqual(api_backend.open_count, 0)

    def test_open_session_reuses_one_worker_for_authenticated_espn_read(self):
        clock = FakeClock()
        session = FakeSession(clock)
        backend = FakeBackend(session)
        collector = BrowserCollector(backend, clock=clock, now=lambda: CAPTURED)
        task = projection_task("espn")
        runtime_url = task.url + "?leagueId=123"
        gate = FakeGate()

        with collector.open_session(
            BrowserCaptureOptions(Path("profile"), action_delay_ms=200),
            sign_in_gate=gate,
        ) as opened:
            league = opened.collect(CapturePlan((league_task(),)))
            payloads = opened.read_authenticated_espn_json(
                task, runtime_url, 2026, "123"
            )
            projection = opened.collect(
                CapturePlan((task,)),
                navigation_bindings={task.task_id: runtime_url},
            )

        self.assertEqual(backend.open_count, 1)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(len(league), 1)
        self.assertEqual(len(projection), 1)
        self.assertEqual(payloads, ({"league": True}, {"schedule": True}))
        self.assertIn(CaptureProvider.ESPN, gate.calls)
        self.assertEqual(
            sum(name == "authenticated_espn" for name, _, _ in session.operations),
            1,
        )

    def test_open_session_verifies_yahoo_scoring_once_and_reuses_provider_gate(self):
        clock = FakeClock()
        session = FakeSession(clock, yahoo_scoring="PPR")
        gate = FakeGate()
        task = projection_task("yahoo")
        runtime = (
            "https://football.fantasysports.yahoo.com/f1/12345/players?status=ALL"
        )
        with BrowserCollector(FakeBackend(session), clock=clock).open_session(
            BrowserCaptureOptions(Path("profile"), action_delay_ms=200),
            sign_in_gate=gate,
        ) as opened:
            self.assertEqual(opened.verify_yahoo_scoring(task, runtime), "PPR")
            opened.collect(
                CapturePlan((task,)), navigation_bindings={task.task_id: runtime}
            )

        self.assertEqual(gate.calls, [CaptureProvider.YAHOO])
        scoring = [value for name, value, _ in session.operations if name == "yahoo_scoring"]
        self.assertEqual(
            scoring,
            [(
                task.task_id,
                "https://football.fantasysports.yahoo.com/2026/f1/12345/settings",
            )],
        )

    def test_yahoo_scoring_mismatch_is_actionable(self):
        task = projection_task("yahoo")
        runtime = (
            "https://football.fantasysports.yahoo.com/f1/12345/players?status=ALL"
        )
        collector = BrowserCollector(FakeBackend(FakeSession(FakeClock(), yahoo_scoring="HALF")))
        with collector.open_session(
            BrowserCaptureOptions(Path("profile")), sign_in_gate=FakeGate()
        ) as opened, self.assertRaisesRegex(
            YahooScoringError, "Half PPR.*set to PPR"
        ):
            opened.verify_yahoo_scoring(task, runtime)

    def test_yahoo_settings_url_requires_a_normalized_same_season_player_list(self):
        self.assertEqual(
            yahoo_settings_url(
                "https://football.fantasysports.yahoo.com/2026/f1/12345/players?status=ALL",
                2026,
            ),
            "https://football.fantasysports.yahoo.com/2026/f1/12345/settings",
        )
        self.assertEqual(
            yahoo_settings_url(
                "https://football.fantasysports.yahoo.com/f1/12345/players?status=ALL",
                2026,
            ),
            "https://football.fantasysports.yahoo.com/2026/f1/12345/settings",
        )
        for value in (
            "https://football.fantasysports.yahoo.com/f1/12345/playersearch?status=ALL",
            "https://football.fantasysports.yahoo.com/f1/12345/players?status=A",
            "https://football.fantasysports.yahoo.com/2025/f1/12345/players?status=ALL",
            "https://evil.example/f1/12345/players?status=ALL",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                yahoo_settings_url(value, 2026)


class PlaywrightAdapterTests(unittest.TestCase):
    def test_one_context_one_page_init_scripts_and_popup_fail_closed(self):
        page, stale = FakePage(), FakePage()
        context = FakeContext((page, stale))
        manager = FakeManager(context)
        backend = PlaywrightCaptureBackend(loader=lambda: (lambda: FakeStarter(manager), FakeTimeoutError))
        session = backend.open(BrowserCaptureOptions(Path("profiles/weekly")))

        self.assertEqual(manager.chromium.open_count, 1)
        self.assertEqual(manager.chromium.options["channel"], "chromium")
        self.assertTrue(stale.closed)
        self.assertEqual(context.init_scripts, [SINGLE_PAGE_SCRIPT, ANALYZER_TAP_SCRIPT])
        popup = FakePage()
        context.handlers["page"](popup)
        self.assertTrue(popup.closed)
        with self.assertRaisesRegex(BrowserCaptureError, "retained page"):
            session.wait_for_events(1)
        session.close()

    def test_analyzer_deadline_checked_under_endless_wrong_candidates(self):
        page = FakePage(endless_candidate={"kind": "body", "value": {"noise": True}})
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        session.begin_analyzer_response_capture(AnalyzerCapturePhase.FULL_PLAYOFFS)
        with self.assertRaises(BrowserCaptureTimeout):
            session.finish_analyzer_response_capture(1, lambda: False)
        session.close()

    def test_analyzer_tap_matches_exact_endpoint_trade_and_full_action_is_allowlisted(self):
        invariants = (
            "url.hostname === 'api.fantasypros.com'",
            "url.pathname === '/v2/ajax/myplaybook.php'",
            "action.includes('tradeanalyzer')",
            "team1gets", "team2gets", "team2id", "queue.push",
        )
        for invariant in invariants:
            self.assertIn(invariant, ANALYZER_TAP_SCRIPT)
        self.assertNotIn("response.url,", TAKE_ANALYZER_BODY_SCRIPT)
        self.assertLess(
            ANALYZER_TAP_SCRIPT.index("request.clone().text()"),
            ANALYZER_TAP_SCRIPT.index("originalFetch.apply"),
        )

        page = FakePage(analyzer_candidates=[{"kind": "body", "value": PLAYOFF_BODY}])
        page.url = "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php"
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        session.begin_analyzer_response_capture(AnalyzerCapturePhase.FULL_PLAYOFFS)
        session.activate_full_analysis(100, lambda: False)
        self.assertEqual(session.finish_analyzer_response_capture(100, lambda: False), PLAYOFF_BODY)
        self.assertEqual(page.full_actions, 1)
        session.close()

    def test_analyzer_bundle_is_discovered_on_page_and_hashed_without_session_data(self):
        page = FakePage()
        page.url = analyzer_task().url
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        with patch(
            "trade_snapshot.bundle_provenance.fetch_analyzer_bundle_fingerprint",
            return_value=CURRENT_BUNDLE_FINGERPRINT,
        ) as fetch:
            first = session.capture_analyzer_bundle(5000, lambda: False)
            second = session.capture_analyzer_bundle(5000, lambda: False)
        self.assertEqual(first, CURRENT_BUNDLE_FINGERPRINT)
        self.assertIs(second, first)
        fetch.assert_called_once_with(
            CURRENT_BUNDLE_FINGERPRINT.url, timeout_seconds=fetch.call_args.kwargs["timeout_seconds"]
        )
        self.assertGreater(fetch.call_args.kwargs["timeout_seconds"], 0)
        session.close()

    def test_final_page_provenance_rejects_redirected_login_or_stale_page(self):
        page = FakePage()
        page.url = "https://secure.fantasypros.com/accounts/login"
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        with self.assertRaisesRegex(BrowserCaptureError, "redirected"):
            task = analyzer_task()
            session.assert_page_provenance(task, task.url, 100, lambda: False)
        session.close()

    def test_projection_paginates_deduplicates_and_proves_source_dimensions(self):
        first = projection_raw("espn", "Player A", "123", "12.4")
        second = projection_raw("espn", "Player B", "456", "10.1")
        page = FakePage(
            projection_values=[first] * 3 + [second] * 6,
            advance_values=[{"action": "next"}, {"action": "done"}],
        )
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        data = session.capture_visible_tables(projection_task("espn"), 5000, 200, lambda: False)

        self.assertEqual(data.segments_captured, 2)
        self.assertEqual(len(data.tables[0].rows), 3)
        self.assertEqual({row[0].text for row in data.tables[0].rows[1:]}, {"Player A", "Player B"})
        self.assertGreaterEqual(page.waited_ms, 200)
        session.close()

    def test_projection_configures_visible_filters_before_extracting(self):
        raw = projection_raw("espn", "Player A", "123", "12.4")
        page = FakePage(
            configuration_values=[
                {"action": "changed", "dimension": "period"}, {"action": "ready"}
            ],
            projection_values=[raw] * 3,
            advance_values=[{"action": "done"}],
        )
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        session.capture_visible_tables(projection_task("espn"), 5000, 200, lambda: False)
        self.assertEqual(page.configuration_requests[0], {
            "provider": "espn", "season": 2026, "week": 1, "horizon": "weekly",
            "scoring": "PPR", "positions": ["RB"],
        })
        self.assertGreaterEqual(page.waited_ms, 200)
        session.close()

    def test_yahoo_scoring_read_checks_navigation_and_stable_visible_result(self):
        page = FakePage(scoring_values=[{"scoring": "HALF"}] * 3)
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        settings = "https://football.fantasysports.yahoo.com/2026/f1/12345/settings"
        self.assertEqual(
            session.read_yahoo_scoring(
                projection_task("yahoo"), settings, 5000, lambda: False
            ),
            "HALF",
        )
        self.assertEqual(page.url, settings)
        with self.assertRaisesRegex(YahooScoringError, "configured safely"):
            session.read_yahoo_scoring(
                projection_task("yahoo"),
                "https://evil.example/f1/12345/settings",
                5000,
                lambda: False,
            )
        self.assertEqual(page.url, settings)
        with self.assertRaisesRegex(YahooScoringError, "configured safely"):
            session.read_yahoo_scoring(
                projection_task("yahoo"),
                "https://football.fantasysports.yahoo.com/f1/12345/settings",
                5000,
                lambda: False,
            )
        session.close()

    def test_yahoo_scoring_read_distinguishes_layout_from_unsupported_scoring(self):
        settings = "https://football.fantasysports.yahoo.com/2026/f1/12345/settings"
        cases = (
            ("receptions_ambiguous", "Settings layout could not be verified"),
            ("unsupported_receptions", "not Standard, Half PPR, or PPR"),
        )
        for error, message in cases:
            with self.subTest(error=error):
                page = FakePage(scoring_values=[{"error": error}] * 3)
                session = _PlaywrightSession(
                    FakeContext((page,)), FakeManager(), FakeTimeoutError
                )
                with self.assertRaisesRegex(YahooScoringError, message):
                    session.read_yahoo_scoring(
                        projection_task("yahoo"), settings, 5000, lambda: False
                    )
                session.close()

    def test_projection_source_evidence_reads_selected_native_options(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional for source-only test runs")
        with sync_playwright() as playwright:
            browser = None
            for channel in ("chromium", "msedge"):
                try:
                    browser = playwright.chromium.launch(channel=channel, headless=True)
                    break
                except Exception:
                    continue
            if browser is None:
                self.skipTest("A Playwright Chromium-family browser is not installed")
            try:
                page = browser.new_page()
                page.route("https://fantasy.espn.com/**", lambda route: route.fulfill(
                    content_type="text/html",
                    body="""<!doctype html><html><body>
                    <select id=season><option selected>2026</option></select>
                    <select id=week><option selected>Week 1</option></select>
                    <select id=scoring><option selected>PPR</option></select>
                    <select id=position><option selected>RB</option></select>
                    <table class=Table><thead><tr><th>PLAYER</th><th>FPTS</th></tr></thead>
                    <tbody><tr><td><a href='https://www.espn.com/nfl/player/_/id/123/a'>A</a></td>
                    <td>12.4</td></tr></tbody></table></body></html>""",
                ))
                page.goto("https://fantasy.espn.com/football/players/projections")
                captured = page.evaluate(PROJECTION_TABLE_SCRIPT, {
                    "provider": "espn", "season": 2026, "week": 1,
                    "horizon": "weekly", "scoring": "PPR", "positions": ["RB"],
                })
            finally:
                browser.close()
        self.assertEqual(captured["source"], {
            "season": 2026, "week": 1, "horizon": "weekly", "scoring": "PPR",
            "positions": ["RB"], "period_text": "2026 | Week 1 | PPR | RB",
        })
        self.assertEqual(len(captured["tables"]), 1)

    def test_yahoo_scoring_script_reads_only_visible_reception_modifier(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional for source-only test runs")
        with sync_playwright() as playwright:
            browser = None
            for channel in ("chromium", "msedge"):
                try:
                    browser = playwright.chromium.launch(channel=channel, headless=True)
                    break
                except Exception:
                    continue
            if browser is None:
                self.skipTest("A Playwright Chromium-family browser is not installed")
            try:
                page = browser.new_page()
                for modifier, expected in (("0", "STD"), ("0.5", "HALF"), ("1", "PPR")):
                    page.set_content(f"""<!doctype html><html><body>
                    <table id=settings-stat-mod-table><tbody>
                    <tr><td>Passing Yards</td><td>0.04</td><td>note</td></tr>
                    <tr><td>Receptions</td><td><b>{modifier}</b></td><td>note</td></tr>
                    </tbody></table></body></html>""")
                    self.assertEqual(
                        page.evaluate(YAHOO_SCORING_SCRIPT), {"scoring": expected}
                    )
                page.set_content("""<!doctype html><table id=settings-stat-mod-table>
                <tr><td>Receptions</td><td>2</td><td>note</td></tr></table>""")
                self.assertEqual(
                    page.evaluate(YAHOO_SCORING_SCRIPT),
                    {"error": "unsupported_receptions"},
                )
                for style, target in (
                    ("visibility:hidden", "table"),
                    ("opacity:0", "table"),
                    ("display:none", "tr"),
                    ("visibility:hidden", "td:first-child"),
                    ("visibility:hidden", "td:nth-child(2)"),
                ):
                    page.set_content(f"""<!doctype html><style>{target}{{{style}}}</style>
                    <table id=settings-stat-mod-table><tr>
                    <td>Receptions</td><td>1</td><td>note</td>
                    </tr></table>""")
                    self.assertIsNone(page.evaluate(YAHOO_SCORING_SCRIPT))
                page.set_content("""<!doctype html><table id=settings-stat-mod-table>
                <tr><td>Receptions</td><td>1</td></tr>
                <tr><td>Receptions</td><td>0.5</td></tr></table>""")
                self.assertEqual(
                    page.evaluate(YAHOO_SCORING_SCRIPT),
                    {"error": "receptions_ambiguous"},
                )
                page.set_content("""<!doctype html>
                <table id=settings-stat-mod-table><tr><td>Receptions</td><td>1</td></tr></table>
                <table id=settings-stat-mod-table><tr><td>Receptions</td><td>1</td></tr></table>
                """)
                self.assertEqual(
                    page.evaluate(YAHOO_SCORING_SCRIPT),
                    {"error": "settings_table_ambiguous"},
                )
            finally:
                browser.close()

    def test_league_source_capture_is_complete_and_sanitized(self):
        value = league_capture_value()
        value["sources"][0]["body"]["payload"].update({
            "leagueKey": "secret", "url": "https://example.test/private",
        })
        page = FakePage(league_value=value)
        page.url = league_task().url
        session = _PlaywrightSession(FakeContext((page,)), FakeManager(), FakeTimeoutError)
        data = session.capture_league_sources(league_task(), 5000, lambda: False)
        self.assertIsInstance(data, LeagueCaptureData)
        self.assertEqual({source.source for source in data.sources}, set(LeagueSourceKind))
        self.assertNotIn("secret", str(data.sources))
        self.assertEqual(page.league_requests, [{
            "timeout_ms": 5000, "expected_season": 2026, "expected_week": 1,
        }])
        session.close()

    def test_live_style_lexical_bootstrap_uses_plan_week_and_rejects_wrong_season(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional for source-only test runs")
        page_data = {
            "season": 2026,
            "league": {
                "key": "runtime-only-private-key", "season": 2026,
                "playoffsTeams": 1, "rosterSize": 14, "scoring": "PPR",
                "settings": {"scoring": "PPR"},
            },
            "teams": [
                {"teamId": 1, "teamName": "One", "players": [{"player_id": 101}]},
                {"teamId": 2, "teamName": "Two", "players": [{"player_id": 102}]},
            ],
            "playerInfo": {
                "101": {"player_id": 101, "player_name": "A", "position_id": 2,
                        "eligibility": ["RB"]},
                "102": {"player_id": 102, "player_name": "B", "position_id": 3,
                        "eligibility": ["WR"]},
            },
        }
        current = [
            {"teamId": 1, "wins": 1, "losses": 0, "ties": 0},
            {"teamId": 2, "wins": 0, "losses": 1, "ties": 0},
        ]
        projected = {"playoffsTeam": 1, "standings": [
            {"teamId": 1, "teamName": "One", "rank_proj": 1, "rank_current": 1,
             "wins_current": 1, "losses_current": 0, "wins_proj": 8,
             "losses_proj": 6, "playoffs_odds": 60, "championship_odds": 20},
            {"teamId": 2, "teamName": "Two", "rank_proj": 2, "rank_current": 2,
             "wins_current": 0, "losses_current": 1, "wins_proj": 6,
             "losses_proj": 8, "playoffs_odds": 40, "championship_odds": 10},
        ]}
        body = """<!doctype html><script>
        const data=Object.freeze(%s);
        window.__tradeSnapshotAnalyzerV2={initQueue:[%s],error:null};
        window.MPB={getProjectedStandings:(_args,ok)=>ok(%s)};
        </script>""" % tuple(map(json.dumps, (
            page_data,
            {"standings": current, "best_free_agents": [{"id": 103}]},
            projected,
        )))
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="chromium", headless=True)
            except Exception:
                self.skipTest("Playwright Chromium is not installed")
            try:
                page = browser.new_page()
                page.route(analyzer_task().url, lambda route: route.fulfill(
                    content_type="text/html", body=body,
                ))
                page.goto(analyzer_task().url)
                captured = page.evaluate(LEAGUE_SOURCE_SCRIPT, {
                    "timeout_ms": 5000, "expected_season": 2026, "expected_week": 7,
                })
                wrong = page.evaluate(LEAGUE_SOURCE_SCRIPT, {
                    "timeout_ms": 5000, "expected_season": 2025, "expected_week": 7,
                })
            finally:
                browser.close()
        bootstrap = next(row for row in captured["sources"] if row["source"] == "bootstrap")
        self.assertEqual(bootstrap["body"]["payload"]["current_week"], 7)
        self.assertNotIn("runtime-only-private-key", json.dumps(captured))
        self.assertEqual(wrong, {"error": "bootstrap_incomplete"})

    def test_league_bootstrap_keeps_position_and_playoff_model_fields(self):
        player_fields = ("position_id", "eligibility", "eligibility_espn", "eligibility_yahoo")
        league_fields = (
            "playoffsTeams", "playoffsStartWeek", "playoffsEndWeek",
            "playoffReseeding", "basic_scoring", "totalRounds",
        )
        for field in player_fields + league_fields:
            self.assertIn(f"'{field}'", LEAGUE_SOURCE_SCRIPT)
        self.assertIn("typeof data", LEAGUE_SOURCE_SCRIPT)
        self.assertIn("tap.initQueue", LEAGUE_SOURCE_SCRIPT)
        self.assertIn("init.includes('y')", ANALYZER_TAP_SCRIPT)
        self.assertIn("getProjectedStandings", LEAGUE_SOURCE_SCRIPT)
        for speculative in (
            "getLeagues", "getAllLeagueRosters", "getLeaguesAndDisplayRoster",
            "getSettings", "getDisplaySettings", "getLeagueLineup", "getLeagueAnalysis",
        ):
            self.assertNotIn(speculative, LEAGUE_SOURCE_SCRIPT)
        body = league_sources()[0].to_record()["body"]
        body["payload"]["players"][0].update({field: field for field in player_fields})
        body["payload"]["league"].update({
            "playoffs_start_week": 15, "playoffs_end_week": 17,
            "playoff_reseeding": True, "basic_scoring": {}, "total_rounds": 14,
        })
        source = LeagueSource("bootstrap", body)
        payload = source.to_record()["body"]["payload"]
        self.assertTrue(set(player_fields) <= set(payload["players"][0]))
        self.assertTrue({
            "playoffs_start_week", "playoffs_end_week", "playoff_reseeding",
            "basic_scoring", "total_rounds",
        } <= set(payload["league"]))

    def test_projection_rejects_private_columns_and_incomplete_evidence(self):
        task = projection_task("yahoo")
        private = projection_raw("yahoo", "Player A", "123", "12.4")
        private["tables"][0]["rows"][0].append(cell("% OWN"))
        private["tables"][0]["rows"][1].append(cell("99%"))
        with self.assertRaisesRegex(BrowserCaptureError, "private|non-allowlisted"):
            projection_capture([private], task)
        for header in ("OWNER", "ROSTERED", "STARTED", "AVAILABILITY"):
            private = projection_raw("yahoo", "Player A", "123", "12.4")
            private["tables"][0]["rows"][0].append(cell(header))
            private["tables"][0]["rows"][1].append(cell("private"))
            with self.subTest(header=header), self.assertRaisesRegex(
                BrowserCaptureError, "private|non-allowlisted"
            ):
                projection_capture([private], task)
        missing = projection_raw("yahoo", "Player A", "123", "12.4")
        missing["source"]["week"] = None
        with self.assertRaisesRegex(BrowserCaptureError, "week"):
            projection_capture([missing], task)

    def test_projection_rejects_partial_position_scope_as_incomplete(self):
        rb_only = projection_raw("espn", "Player A", "123", "12.4")
        for requested in (("RB", "WR"), ("ALL",), ("FLX",)):
            with self.subTest(requested=requested):
                with self.assertRaisesRegex(BrowserCaptureError, "positions"):
                    projection_capture([rb_only], projection_task("espn", requested))

    def test_current_ecr_bootstrap_contract_uses_numeric_filters_and_source_dimensions(self):
        task = ecr_task(expected=False)
        raw = ecr_raw(expert_count=19)
        data = ecr_capture_data(raw, task)
        self.assertEqual(data.expert_count, 19)
        self.assertTrue(all(value.isdigit() for value in data.expert_ids))
        self.assertEqual(data.rankings[0].provider_player_id, "22968")
        self.assertEqual(data.rankings[0].position_rank, "RB1")
        self.assertEqual(data.last_updated_text, "9/01")

        wrong = ecr_raw(expert_count=19)
        wrong["source"]["ranking_type"] = "draft"
        with self.assertRaisesRegex(BrowserCaptureError, "horizon"):
            ecr_capture_data(wrong, task)
        wrong = ecr_raw(expert_count=19)
        wrong["source"]["year"] = "2025"
        with self.assertRaisesRegex(BrowserCaptureError, "season"):
            ecr_capture_data(wrong, task)

    def test_export_mode_is_rejected_in_schema_not_masked_by_fake_dom(self):
        with self.assertRaises(ValueError):
            ecr_task(method="export")


class WorkerBoundaryTests(unittest.TestCase):
    def test_frozen_capture_values_cross_the_spawned_process_as_plain_records(self):
        values = {
            "capture_ecr_rankings": ECRCaptureData(
                tuple(str(index) for index in range(1, 20)),
                19,
                "9/01",
                None,
                (ecr_row(),),
            ),
            "capture_league_sources": LeagueCaptureData(2, league_sources()),
            "capture_visible_tables": ProjectionCaptureData(
                (
                    VisibleTable(
                        (
                            (VisibleTableCell("PLAYER"), VisibleTableCell("FPTS")),
                            (
                                VisibleTableCell(
                                    "Player A",
                                    ("https://www.fantasypros.com/nfl/players/player-a.php",),
                                ),
                                VisibleTableCell("12.4"),
                            ),
                        )
                    ),
                ),
                "2026 | Week 1 | PPR | RB",
                1,
            ),
            "read_authenticated_espn_json": (
                {"league": {"id": 123}},
                {"settings": {"schedule": True}},
            ),
            "read_yahoo_scoring": "HALF",
        }
        for operation, expected in values.items():
            with self.subTest(operation=operation):
                wire = _encode_result(operation, expected)
                transferred = pickle.loads(pickle.dumps(wire))
                self.assertEqual(_decode_result(operation, transferred), expected)

    def test_worker_decoder_rejects_untyped_capture_payloads(self):
        for operation in (
            "capture_ecr_rankings",
            "capture_league_sources",
            "capture_visible_tables",
            "read_authenticated_espn_json",
            "read_yahoo_scoring",
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                BrowserCaptureError, "invalid"
            ):
                _decode_result(operation, {"unexpected": True})

    def test_blocking_rpc_is_killed_on_deadline_and_cancellation(self):
        process = FakeProcess()
        session = _WorkerSession(BlockingConnection(), process)
        with self.assertRaises(BrowserCaptureTimeout):
            session._receive(1, lambda: False, "evaluate")
        self.assertTrue(process.terminated)

        process = FakeProcess()
        session = _WorkerSession(BlockingConnection(), process)
        with self.assertRaises(BrowserCaptureCancelled):
            session._receive(1000, lambda: True, "evaluate")
        self.assertTrue(process.terminated)

    def test_lazy_dependency_error_remains_clear(self):
        def missing():
            raise BrowserCaptureDependencyError("install playwright and Chromium")

        backend = PlaywrightCaptureBackend(loader=missing)
        with self.assertRaisesRegex(BrowserCaptureDependencyError, "install playwright"):
            backend.open(BrowserCaptureOptions(Path("profile")))


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeBackend:
    def __init__(self, session):
        self.session, self.open_count = session, 0

    def open(self, options, timeout_ms, cancelled):
        self.open_count += 1
        if cancelled():
            raise BrowserCaptureCancelled("cancelled")
        return self.session


class FakeSession:
    def __init__(self, clock, navigation_duration=0, yahoo_scoring="PPR"):
        self.clock, self.navigation_duration = clock, navigation_duration
        self.yahoo_scoring = yahoo_scoring
        self.operations, self.action_times = [], []
        self.phase, self.close_count = None, 0

    def _op(self, name, value=None, action=False):
        self.operations.append((name, value, self.clock()))
        if action:
            self.action_times.append(self.clock())

    def begin_analyzer_response_capture(self, phase):
        self.phase = phase
        self._op("arm", phase)

    def navigate(self, url, timeout_ms, cancelled):
        self._op("navigate", url, True)
        self.clock.sleep(self.navigation_duration)

    def assert_page_provenance(self, task, planned_url, timeout_ms, cancelled):
        self._op("provenance", task.task_id)

    def activate_full_analysis(self, timeout_ms, cancelled):
        self._op("full", None, True)

    def finish_analyzer_response_capture(self, timeout_ms, cancelled):
        self._op("analyzer", self.phase)
        return PLAYOFF_BODY if self.phase is AnalyzerCapturePhase.FULL_PLAYOFFS else POWER_BODY

    def abort_analyzer_response_capture(self):
        self._op("abort")
        self.phase = None

    def capture_analyzer_bundle(self, timeout_ms, cancelled):
        self._op("bundle")
        return CURRENT_BUNDLE_FINGERPRINT

    def capture_visible_tables(self, task, timeout_ms, action_delay_ms, cancelled):
        self._op("tables", task.provider, True)
        link = {
            CaptureProvider.FANTASYPROS: "https://www.fantasypros.com/nfl/players/player-a.php",
            CaptureProvider.ESPN: "https://www.espn.com/nfl/player/_/id/123/player-a",
            CaptureProvider.YAHOO: "https://sports.yahoo.com/nfl/players/456/",
        }[task.provider]
        table = VisibleTable((
            (VisibleTableCell("PLAYER"), VisibleTableCell("FPTS")),
            (VisibleTableCell("Player A", (link,)), VisibleTableCell("12.4")),
        ))
        return ProjectionCaptureData((table,), "2026 | Week 1 | PPR | RB", 1)

    def capture_ecr_rankings(self, task, timeout_ms, cancelled):
        self._op("ecr", task.horizon, True)
        return ECRCaptureData(
            tuple(str(index) for index in range(1, 20)), 19, "9/01", None,
            (ecr_row(),),
        )

    def capture_league_sources(self, task, timeout_ms, cancelled):
        self._op("league", None, True)
        return LeagueCaptureData(2, league_sources())

    def read_authenticated_espn_json(
        self, season, league_id, timeout_ms, maximum_bytes, cancelled
    ):
        self._op("authenticated_espn", (season, league_id), True)
        if cancelled():
            raise BrowserCaptureCancelled("cancelled")
        return {"league": True}, {"schedule": True}

    def read_yahoo_scoring(self, task, settings_url, timeout_ms, cancelled):
        self._op("yahoo_scoring", (task.task_id, settings_url), True)
        if cancelled():
            raise BrowserCaptureCancelled("cancelled")
        return self.yahoo_scoring

    def wait_for_events(self, timeout_ms):
        self.clock.sleep(timeout_ms / 1000)

    def close(self, timeout_ms=5000):
        self.close_count += 1


class FakeGate:
    def __init__(self):
        self.calls = []

    def is_ready(self, task):
        self.calls.append(task.provider)
        return True


class FakeTimeoutError(Exception):
    pass


class FakePage:
    def __init__(
        self, *, analyzer_candidates=None, endless_candidate=None, projection_values=None,
        advance_values=None, ecr_values=None, configuration_values=None, league_value=None,
        scoring_values=None,
    ):
        self.closed, self.handlers = False, {}
        self.analyzer_candidates = list(analyzer_candidates or [])
        self.endless_candidate = endless_candidate
        self.projection_values = list(projection_values or [])
        self.advance_values = list(advance_values or [])
        self.ecr_values = list(ecr_values or [])
        self.configuration_values = list(configuration_values or [{"action": "ready"}])
        self.configuration_requests, self.league_requests = [], []
        self.league_value = league_value
        self.scoring_values = list(scoring_values or [])
        self.main_frame, self.url = object(), "https://www.espn.com/"
        self.full_actions, self.waited_ms = 0, 0

    def close(self, **kwargs):
        self.closed = True

    def is_closed(self):
        return self.closed

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        if self.handlers.get(event) is handler:
            del self.handlers[event]

    def goto(self, url, **kwargs):
        self.url = url
        if handler := self.handlers.get("framenavigated"):
            handler(self.main_frame)

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, timeout_ms):
        self.waited_ms += timeout_ms
        time.sleep(min(timeout_ms / 1000, 0.002))

    def evaluate(self, script, argument=None):
        if script == CONFIGURE_PROJECTION_SCRIPT:
            self.configuration_requests.append(argument)
            return next_or_last(self.configuration_values)
        if script == TAKE_ANALYZER_BODY_SCRIPT:
            return self.analyzer_candidates.pop(0) if self.analyzer_candidates else self.endless_candidate
        if script == ANALYZER_BUNDLE_SOURCE_SCRIPT:
            return CURRENT_BUNDLE_FINGERPRINT.url
        if script == FULL_ANALYSIS_ACTION_SCRIPT:
            self.full_actions += 1
            return {"clicked": True}
        if script == PAGE_PROVENANCE_SCRIPT:
            parsed = urlsplit(self.url)
            return {"protocol": parsed.scheme + ":", "hostname": parsed.hostname,
                    "port": str(parsed.port or ""), "pathname": parsed.path}
        if script == PROJECTION_TABLE_SCRIPT:
            return next_or_last(self.projection_values)
        if script == ADVANCE_PROJECTION_SCRIPT:
            return next_or_last(self.advance_values)
        if script == ECR_TABLE_SCRIPT:
            return next_or_last(self.ecr_values)
        if script == LEAGUE_SOURCE_SCRIPT:
            self.league_requests.append(argument)
            return self.league_value
        if script == YAHOO_SCORING_SCRIPT:
            return next_or_last(self.scoring_values)
        raise AssertionError("unexpected script")


class FakeContext:
    def __init__(self, pages):
        self.pages, self.handlers, self.init_scripts = list(pages), {}, []

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def on(self, event, handler):
        self.handlers[event] = handler

    def add_init_script(self, *, script):
        self.init_scripts.append(script)

    def close(self):
        pass


class FakeManager:
    def __init__(self, context=None):
        self.chromium = FakeChromium(context) if context else None

    def stop(self):
        pass


class FakeStarter:
    def __init__(self, manager):
        self.manager = manager

    def start(self):
        return self.manager


class FakeChromium:
    def __init__(self, context):
        self.context, self.open_count, self.options = context, 0, None

    def launch_persistent_context(self, *args, **kwargs):
        self.open_count += 1
        self.options = kwargs
        return self.context


class BlockingConnection:
    def poll(self, timeout):
        time.sleep(timeout)
        return False

    def close(self):
        pass


class FakeProcess:
    def __init__(self):
        self.alive, self.terminated = True, False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated, self.alive = True, False

    def join(self, timeout):
        pass


def make_plan():
    return CapturePlan((
        analyzer_task(), ecr_task(), projection_task("fantasypros"),
        projection_task("espn"), projection_task("yahoo"), league_task(),
    ))


def analyzer_task():
    return PageCaptureTask(
        "fantasypros", 2026, 1, "analyzer_response",
        "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
        "full_playoffs", AnalyzerTradeSpec(2, (1001, 1002), (2001, 2002)),
    )


def league_task():
    return PageCaptureTask(
        "fantasypros", 2026, 1, "league_source",
        "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
    )


def projection_task(provider, positions=("RB",)):
    url = {
        "fantasypros": "https://www.fantasypros.com/nfl/projections/rb.php",
        "espn": "https://fantasy.espn.com/football/players/projections",
        "yahoo": "https://football.fantasysports.yahoo.com/f1/players",
    }[provider]
    return PageCaptureTask(
        provider, 2026, 1, "visible_table", url,
        projection=ProjectionTableSpec("weekly", "PPR", positions),
    )


def ecr_task(expected=False, method="visible_page"):
    return FantasyProsECRTask(
        2026, 1, "weekly", "PPR", ("RB",),
        tuple(str(index) for index in range(1, 20)) if expected else (),
        19 if expected else None,
        "https://www.fantasypros.com/nfl/rankings/ppr-rb.php", method,
    )


def projection_raw(provider, player, player_id, points):
    link = {
        "fantasypros": f"https://www.fantasypros.com/nfl/players/player-{player_id}.php",
        "espn": f"https://www.espn.com/nfl/player/_/id/{player_id}/player",
        "yahoo": f"https://sports.yahoo.com/nfl/players/{player_id}/",
    }[provider]
    return {
        "source": {
            "season": 2026, "week": 1, "horizon": "weekly", "scoring": "PPR",
            "positions": ["RB"], "period_text": "2026 | Week 1 | PPR | RB",
        },
        "tables": [{"rows": [
            [cell("PLAYER"), cell("FPTS")],
            [cell(player, [link]), cell(points)],
        ]}],
    }


def ecr_raw(expert_count=19):
    return {
        "source": {
            "sport": "NFL", "ranking_type": "weekly", "type_text": "Weekly PPR",
            "year": "2026", "week": "1", "position": "RB", "scoring": "PPR",
            "expert_ids": [str(index) for index in range(1, expert_count + 1)],
            "expert_count": expert_count, "last_updated": "9/01", "player_count": 1,
        },
        "rankings": [{
            "player_id": 22968, "player_name": "Player A", "team": "DET",
            "position": "RB", "rank_ecr": 1, "rank_min": "1", "rank_max": "3",
            "rank_avg": "2", "rank_std": "0.8", "position_rank": "RB1",
        }],
    }


def ecr_row():
    return ECRRankingRow(
        "22968", "Player A", "DET", "RB", 1, 1, 3, 2, 0.8, "RB1",
        {"ECR": "1", "BEST": "1", "WORST": "3", "AVG": "2", "STD DEV": "0.8"},
    )


def cell(text, links=()):
    return {"text": text, "links": list(links)}


def next_or_last(values):
    if not values:
        return None
    return values.pop(0) if len(values) > 1 else values[0]


if __name__ == "__main__":
    unittest.main()
