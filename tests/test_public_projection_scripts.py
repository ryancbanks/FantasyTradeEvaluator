import unittest

from trade_snapshot._capture_scripts import (
    ADVANCE_PROJECTION_SCRIPT,
    CONFIGURE_PROJECTION_SCRIPT,
    PROJECTION_TABLE_SCRIPT,
)
from trade_snapshot._projection_parse import projection_artifact_rows
from trade_snapshot._projection_tables import projection_capture
from trade_snapshot.capture_schema import (
    CaptureKind,
    CaptureProvider,
    GenericTableArtifact,
    PageCaptureTask,
    ProjectionTableSpec,
    RankingHorizon,
)


FFTODAY_WEEKLY = "https://www.fftoday.com/rankings/playerwkproj.php"
FFTODAY_SEASON = "https://www.fftoday.com/rankings/playerproj.php"
FANTASYSHARKS = "https://www.fantasysharks.com/apps/bert/forecasts/projections.php"
CBS_SEASON = "https://www.cbssports.com/fantasy/football/stats"


def projection_task(provider, *, season=2026, week=1, horizon="weekly", position="QB"):
    urls = {
        "cbs": f"{CBS_SEASON}/{position}/{season}/season/projections/ppr/",
        "fftoday": FFTODAY_WEEKLY if horizon == "weekly" else FFTODAY_SEASON,
        "fantasysharks": FANTASYSHARKS,
    }
    return PageCaptureTask(
        provider,
        season,
        week,
        "visible_table",
        urls[provider],
        projection=ProjectionTableSpec(horizon, "PPR", (position,)),
    )


def request_for(task):
    return {
        "provider": task.provider.value,
        "season": task.season,
        "week": task.week,
        "horizon": task.projection.horizon.value,
        "scoring": task.projection.scoring,
        "positions": list(task.projection.position_scope),
    }


def artifact_from(captured, task):
    capture = projection_capture([captured], task)
    return GenericTableArtifact(
        task_id=task.task_id,
        provider=task.provider,
        season=task.season,
        week=task.week,
        kind=CaptureKind.VISIBLE_TABLE,
        captured_at="2026-09-02T12:00:00Z",
        horizon=task.projection.horizon,
        scoring=task.projection.scoring,
        position_scope=task.projection.position_scope,
        source_period_text=capture.source_period_text,
        segments_captured=capture.segments_captured,
        complete=True,
        tables=capture.tables,
    )


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


class PublicProjectionScriptTests(unittest.TestCase):
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

    def open_page(self, url, body):
        page = self._browser.new_page()
        origin = url.split("/", 3)[:3]
        page.route("/".join(origin) + "/**", lambda route: route.fulfill(
            content_type="text/html", body=body,
        ))
        page.goto(url)
        self.addCleanup(page.close)
        return page

    @staticmethod
    def cbs_body(season):
        return f"""<!doctype html><body>
          <h1>{season} Projections Fantasy Football Quarterback Stats</h1>
          <table class='TableBase-table'><thead><tr>
            <th>Player</th><th>GP</th><th>FPTS</th><th>FPPG</th>
          </tr></thead><tbody><tr>
            <td><a href='https://www.cbssports.com/nfl/players/2181054/josh-allen/fantasy/'>Josh Allen</a> QB BUF</td>
            <td>17</td><td>412.3</td><td>24.3</td>
          </tr></tbody></table>
        </body>"""

    def test_cbs_visible_heading_proves_matching_season(self):
        task = projection_task("cbs", horizon="ros")
        page = self.open_page(task.url, self.cbs_body(2026))

        captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request_for(task))

        self.assertEqual(captured["source"]["season"], 2026)
        self.assertEqual(captured["source"]["positions"], ["QB"])
        rows = projection_artifact_rows(artifact_from(captured, task))
        self.assertEqual(rows[0].display_name, "Josh Allen")

    def test_cbs_rejects_url_season_when_visible_heading_disagrees(self):
        task = projection_task("cbs", season=2025, horizon="ros")
        page = self.open_page(task.url, self.cbs_body(2026))

        captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request_for(task))

        self.assertIsNone(captured["source"]["season"])
        self.assertIsNone(captured["source"]["horizon"])
        self.assertEqual(captured["source"]["positions"], [])

    def test_fftoday_unavailable_week_fails_immediately(self):
        task = projection_task("fftoday")
        url = FFTODAY_WEEKLY + "?LeagueID=107644&PosID=10&Season=2026&GameWeek=1"
        page = self.open_page(url, """<!doctype html><body>
          <p>We hope to have this problem fixed soon.</p>
          <p style='color:red'>No Player Found!</p>
        </body>""")

        self.assertEqual(
            page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request_for(task)),
            {"action": "error", "dimension": "fftoday availability"},
        )

    def test_fftoday_rejects_uncapturable_position_horizon_pairs(self):
        page = self.open_page(
            FFTODAY_WEEKLY
            + "?LeagueID=107644&PosID=10&Season=2026&GameWeek=1",
            "<!doctype html><body><p>Projection fixture</p></body>",
        )
        cases = (
            ("weekly", "DL"),
            ("weekly", "LB"),
            ("weekly", "DB"),
            ("weekly", "DST"),
            ("ros", "DST"),
        )
        for horizon, position in cases:
            with self.subTest(horizon=horizon, position=position):
                request = {
                    "provider": "fftoday",
                    "season": 2026,
                    "week": 1,
                    "horizon": horizon,
                    "scoring": "PPR",
                    "positions": [position],
                }

                self.assertEqual(
                    page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request),
                    {"action": "error", "dimension": "fftoday request"},
                )

    def test_fftoday_read_does_not_verify_unsupported_weekly_idp_source(self):
        url = (
            FFTODAY_WEEKLY
            + "?LeagueID=107644&PosID=50&Season=2026&GameWeek=1"
        )
        body = """<!doctype html><body>
          <div class='bodycontent'>2026 Week 1</div>
          <table><tr class='tableclmhdr'>
            <th>Player Sort First: Last:</th><th>Team</th><th>Opp</th><th>FPts</th>
          </tr><tr>
            <td><a href='https://www.fftoday.com/stats/players/501/Example_Player'>Example Player</a></td>
            <td>BUF</td><td>MIA</td><td>8.0</td>
          </tr></table>
        </body>"""
        page = self.open_page(url, body)
        request = {
            "provider": "fftoday",
            "season": 2026,
            "week": 1,
            "horizon": "weekly",
            "scoring": "PPR",
            "positions": ["DL"],
        }

        captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request)
        self.assertEqual(captured["source"]["positions"], [])
        self.assertIsNone(captured["source"]["horizon"])

    def test_fftoday_weekly_kicker_headers_and_dotted_link_are_typed(self):
        task = projection_task(
            "fftoday", season=2025, week=1, position="K"
        )
        url = FFTODAY_WEEKLY + "?LeagueID=107644&PosID=80&Season=2025&GameWeek=1"
        body = """<!doctype html><body>
          <div class='bodycontent'>2025 Week 1</div>
          <table>
            <tr class='tablehdr'><th colspan='10'>2025 Week 1</th></tr>
            <tr class='tableclmhdr'>
              <th>Chg</th><th>Player Sort First: Last:</th><th>Team</th><th>Opp</th>
              <th>FG Made</th><th>FG Miss</th><th>XP Made</th><th>XP Miss</th><th>FFPts</th>
            </tr>
            <tr><td>-</td><td><a href='https://www.fftoday.com/stats/players/501/J.A._Bates'>Jake Bates</a></td>
              <td>DET</td><td>GB</td><td>2</td><td>1</td><td>3</td><td>0</td><td>9.0</td></tr>
          </table>
        </body>"""
        page = self.open_page(url, body)

        captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request_for(task))
        headers = [cell["text"] for cell in captured["tables"][0]["rows"][0]]
        self.assertEqual(
            headers,
            ["PLAYER", "TEAM", "OPP", "FGM", "FGM MISS", "XPM", "XPM MISS", "FPTS"],
        )
        rows = projection_artifact_rows(artifact_from(captured, task))
        self.assertEqual(rows[0].display_name, "Jake Bates")
        self.assertEqual(rows[0].projected_fantasy_points, 9.0)
        self.assertEqual(dict(rows[0].raw_projected_stats)["fgm_miss"], 1.0)

    def test_fftoday_single_header_rows_get_position_aware_stat_groups(self):
        cases = (
            (
                "QB",
                "10",
                ("Cmp", "Att", "Yds", "TD", "INT", "Att", "Yds", "TD"),
                ("24", "35", "280", "2", "1", "6", "42", "1"),
                (
                    "CMP", "PASS ATT", "PASS YDS", "PASS TD", "INT",
                    "RUSH ATT", "RUSH YDS", "RUSH TD",
                ),
            ),
            (
                "RB",
                "20",
                ("Att", "Yds", "TD", "Rec", "Yds", "TD"),
                ("220", "1050", "8", "55", "430", "3"),
                ("RUSH ATT", "RUSH YDS", "RUSH TD", "REC", "REC YDS", "REC TD"),
            ),
            (
                "WR",
                "30",
                ("Rec", "Yds", "TD", "Att", "Yds", "TD"),
                ("95", "1320", "10", "4", "24", "0"),
                ("REC", "REC YDS", "REC TD", "RUSH ATT", "RUSH YDS", "RUSH TD"),
            ),
            (
                "TE",
                "40",
                ("Rec", "Yds", "TD"),
                ("80", "920", "7"),
                ("REC", "REC YDS", "REC TD"),
            ),
        )
        for position, position_id, source_headers, values, expected_stats in cases:
            with self.subTest(position=position):
                task = projection_task("fftoday", horizon="ros", position=position)
                url = (
                    FFTODAY_SEASON
                    + f"?LeagueID=107644&PosID={position_id}&Season=2026"
                )
                headers = "".join(f"<th>{header}</th>" for header in source_headers)
                cells = "".join(f"<td>{value}</td>" for value in values)
                body = f"""<!doctype html><body>
                  <div class='bodycontent'>2026 Regular Season</div>
                  <table><tr class='tableclmhdr'>
                    <th>Chg</th><th>Player Sort First: Last:</th><th>Tm</th><th>Bye</th>
                    {headers}<th>FPts</th>
                  </tr><tr><td>-</td>
                    <td><a href='https://www.fftoday.com/stats/players/501/A.J._Example'>A.J. Example</a></td>
                    <td>BUF</td><td>7</td>{cells}<td>250.4</td>
                  </tr></table>
                </body>"""
                page = self.open_page(url, body)

                captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request_for(task))
                captured_headers = tuple(
                    cell["text"] for cell in captured["tables"][0]["rows"][0]
                )
                self.assertEqual(
                    captured_headers,
                    ("PLAYER", "TM", "BYE", *expected_stats, "FPTS"),
                )
                rows = projection_artifact_rows(artifact_from(captured, task))
                self.assertEqual(rows[0].display_name, "A.J. Example")
                self.assertEqual(rows[0].projected_fantasy_points, 250.4)

    def test_fantasysharks_weekly_preserves_matchup_and_numeric_opportunities(self):
        task = projection_task("fantasysharks")
        body = """<!doctype html><body>
          <select name='Segment'>
            <option value='season'>2026 NFL Season</option>
            <option value='week1' selected>Week 1</option>
          </select>
          <select name='Position'><option value='1' selected>QB</option></select>
          <select name='scoring'><option value='2' selected>PPR</option></select>
          <table id='toolData'><thead><tr>
            <th>#</th><th>Player</th><th>Tm</th><th>Opp</th><th>Att</th><th>Comp</th>
            <th>Pass Yds</th><th>Pass TDs</th><th>Int</th><th>Rush</th><th>Rsh Yds</th>
            <th>Rsh TDs</th><th>Fum</th><th>Opp</th><th>Pts</th>
          </tr></thead><tbody>
            <tr><td>1</td><td><a href='https://www.fantasysharks.com/apps/bert/players/playerpage.php?id=42'>Allen, Josh</a></td>
              <td>BUF</td><td>vs. MIA</td><td>32</td><td>22</td><td>275</td><td>2</td>
              <td>1</td><td>6</td><td>38</td><td>1</td><td>0</td><td>64</td><td>25.8</td></tr>
            <tr><th>#</th><th>Player</th><th>Tm</th><th>Opp</th><th>Att</th><th>Comp</th>
              <th>Pass Yds</th><th>Pass TDs</th><th>Int</th><th>Rush</th><th>Rsh Yds</th>
              <th>Rsh TDs</th><th>Fum</th><th>Opp</th><th>Pts</th></tr>
          </tbody></table>
        </body>"""
        page = self.open_page(FANTASYSHARKS, body)

        captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request_for(task))
        headers = [cell["text"] for cell in captured["tables"][0]["rows"][0]]
        self.assertEqual(headers.count("OPP"), 1)
        self.assertIn("SCORING OPPORTUNITIES", headers)
        self.assertIn("CMP", headers)
        self.assertIn("RUSH ATT", headers)
        rows = projection_artifact_rows(artifact_from(captured, task))
        self.assertEqual(rows[0].display_name, "Josh Allen")
        self.assertEqual(rows[0].opponent_team_id, "MIA")
        self.assertTrue(rows[0].is_home)
        self.assertEqual(
            dict(rows[0].raw_projected_stats)["scoring_opportunities"], 64.0
        )

    def test_fantasysharks_ros_opp_is_a_stat_not_a_matchup(self):
        task = projection_task("fantasysharks", horizon="ros")
        body = """<!doctype html><body>
          <select name='Segment'>
            <option value='season'>2026 NFL Season</option>
            <option value='ros' selected>2026 Rest of Year</option>
          </select>
          <select name='Position'><option value='1' selected>QB</option></select>
          <select name='scoring'><option value='2' selected>PPR</option></select>
          <table id='toolData'><thead><tr>
            <th>#</th><th>Player</th><th>Tm</th><th>Att</th><th>Pass Yds</th>
            <th>Opp</th><th>Pts</th>
          </tr></thead><tbody><tr>
            <td>1</td><td><a href='https://www.fantasysharks.com/apps/bert/players/playerpage.php?id=42'>Allen, Josh</a></td>
            <td>BUF</td><td>500</td><td>4200</td><td>610</td><td>330.1</td>
          </tr></tbody></table>
        </body>"""
        page = self.open_page(FANTASYSHARKS, body)

        captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request_for(task))
        headers = [cell["text"] for cell in captured["tables"][0]["rows"][0]]
        self.assertNotIn("OPP", headers)
        self.assertIn("SCORING OPPORTUNITIES", headers)
        row = projection_artifact_rows(artifact_from(captured, task))[0]
        self.assertIsNone(row.opponent_team_id)
        self.assertEqual(
            dict(row.raw_projected_stats)["scoring_opportunities"], 610.0
        )

    def test_fftoday_pagination_accepts_only_exact_sequential_sorted_link(self):
        current = FFTODAY_SEASON + "?LeagueID=107644&PosID=10&Season=2026"
        valid = (
            FFTODAY_SEASON
            + "?Season=2026&PosID=10&LeagueID=107644&order_by=FFPts&sort_order=DESC&cur_page=1"
        )
        cases = (
            (valid, "next"),
            (valid.replace("cur_page=1", "cur_page=2"), "error"),
            (valid.replace("sort_order=DESC", "sort_order=ASC"), "error"),
            (valid + "&unexpected=1", "error"),
            (valid + "&cur_page=1", "error"),
        )
        for destination, action in cases:
            with self.subTest(destination=destination):
                body = f"""<!doctype html><body><main>
                  <table><tr class='tableclmhdr'><th>Player</th><th>FPts</th></tr>
                    <tr><td>Example</td><td>1</td></tr></table>
                  <a href='{destination}'>Next Page</a>
                </main></body>"""
                page = self.open_page(current, body)
                self.assertEqual(
                    page.evaluate(ADVANCE_PROJECTION_SCRIPT, "fftoday"),
                    {"action": action},
                )


if __name__ == "__main__":
    unittest.main()
