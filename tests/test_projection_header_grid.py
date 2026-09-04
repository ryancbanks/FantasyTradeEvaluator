from pathlib import Path
import unittest

from trade_snapshot._capture_scripts import PROJECTION_TABLE_SCRIPT
from trade_snapshot._projection_parse import projection_artifact_rows
from trade_snapshot._projection_config_script import CONFIGURE_PROJECTION_SCRIPT
from trade_snapshot._projection_script import PROJECTION_PAGE_SCRIPT
from trade_snapshot._projection_tables import projection_capture
from trade_snapshot._capture_task_policy import fantasypros_projection_url
from trade_snapshot.browser_capture import BrowserCaptureError
from trade_snapshot.capture_schema import (
    CaptureKind,
    GenericTableArtifact,
    PageCaptureTask,
    ProjectionTableSpec,
    public_player_link,
)
from trade_snapshot.source_plan import build_weekly_source_plan


CAPTURED_AT = "2026-09-01T14:15:16Z"


class ProjectionHeaderGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright is optional for source-only test runs")
        cls._playwright_context = sync_playwright()
        cls._playwright = cls._playwright_context.start()
        cls._browser = _launch_browser(cls._playwright, cls)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_browser"):
            cls._browser.close()
        if hasattr(cls, "_playwright"):
            cls._playwright.stop()

    def test_live_shaped_fantasypros_table_reaches_typed_projection(self):
        plan = build_weekly_source_plan(
            season=2026,
            as_of_week=1,
            remaining_weeks=(1,),
            scoring="PPR",
            player_positions=("QB",),
            include_future_weekly=False,
        )
        task = next(
            task for task in plan.tasks
            if isinstance(task, PageCaptureTask)
            and task.kind is CaptureKind.VISIBLE_TABLE
            and task.provider.value == "fantasypros"
        )
        self.assertEqual(
            task.url,
            "https://www.fantasypros.com/nfl/projections/qb.php?week=1&scoring=PPR",
        )
        capture = self._capture(task, _FANTASYPROS_QB)

        self.assertEqual(capture["source"], {
            "season": 2026,
            "week": 1,
            "horizon": "weekly",
            "scoring": "PPR",
            "positions": ["QB"],
            "period_text": "2026 | Week 1 | PPR | QB",
        })
        self.assertEqual(
            _headers(capture),
            (
                "PLAYER",
                "PASS ATT",
                "PASS CMP",
                "PASS YDS",
                "PASS TD",
                "PASS INT",
                "RUSH ATT",
                "RUSH YDS",
                "RUSH TD",
                "MISC FL",
                "FPTS",
            ),
        )
        row = _typed_rows(task, capture)[0]
        self.assertEqual(
            (
                row.identity_provider,
                row.provider_player_id,
                row.display_name,
                row.position,
                row.nfl_team_id,
            ),
            ("fantasypros_projection", "jalen-hurts", "Jalen Hurts", "QB", "PHI"),
        )
        self.assertEqual(
            dict(row.raw_projected_stats),
            {
                "pass_att": 34.2,
                "pass_cmp": 22.8,
                "pass_yds": 258.4,
                "pass_td": 1.8,
                "pass_int": 0.7,
                "rush_att": 8.1,
                "rush_yds": 42.3,
                "rush_td": 0.5,
                "misc_fl": 0.2,
            },
        )
        self.assertEqual(row.projected_fantasy_points, 24.1)
        self.assertEqual(
            capture["tables"][0]["rows"][1][0]["links"],
            ["https://www.fantasypros.com/nfl/projections/jalen-hurts.php"],
        )

    def test_three_level_espn_grid_resolves_row_and_column_spans(self):
        task = _task("espn", "RB")
        capture = self._capture(task, _ESPN_RB)

        self.assertEqual(
            _headers(capture),
            (
                "PLAYER",
                "TEAM",
                "POS",
                "STATUS",
                "RUSH ATT",
                "RUSH YDS",
                "RUSH TD",
                "REC",
                "REC TD",
                "FPTS",
            ),
        )
        row = _typed_rows(task, capture)[0]
        self.assertEqual(row.provider_player_id, "3929630")
        self.assertEqual(row.display_name, "Saquon Barkley")
        self.assertEqual(row.provider_status_designation, "Questionable")
        self.assertEqual(
            dict(row.raw_projected_stats),
            {
                "rush_att": 19.4,
                "rush_yds": 91.7,
                "rush_td": 0.8,
                "rec": 3.2,
                "rec_td": 0.2,
            },
        )

    def test_yahoo_grid_keeps_group_semantics_with_row_spans(self):
        task = _task("yahoo", "WR")
        capture = self._capture(task, _YAHOO_WR)

        self.assertEqual(
            _headers(capture),
            (
                "PLAYER",
                "BYE",
                "FAN PTS",
                "RUSH ATT",
                "RUSH YDS",
                "REC TGT",
                "REC",
                "REC YDS",
                "REC TD",
            ),
        )
        row = _typed_rows(task, capture)[0]
        self.assertEqual(row.provider_player_id, "32687")
        self.assertEqual(
            dict(row.raw_projected_stats),
            {
                "rush_att": 0.4,
                "rush_yds": 2.1,
                "rec_tgt": 9.2,
                "rec": 6.3,
                "rec_yds": 84.6,
                "rec_td": 0.6,
            },
        )

    def test_fantasypros_kicker_keeps_each_live_stat_unit(self):
        task = _task("fantasypros", "K")
        capture = self._capture(task, _FANTASYPROS_K)

        self.assertEqual(
            _headers(capture),
            ("PLAYER", "FG", "FGA", "XPT", "FPTS"),
        )
        row = _typed_rows(task, capture)[0]
        self.assertEqual(row.display_name, "Cameron Dicker")
        self.assertEqual(row.position, "K")
        self.assertEqual(row.nfl_team_id, "LAC")
        self.assertEqual(
            dict(row.raw_projected_stats),
            {"fg": 1.9, "fga": 2.3, "xpt": 2.8},
        )

    def test_fantasypros_future_empty_or_previous_season_page_is_not_published(self):
        for week, body in (
            (2, _FANTASYPROS_QB_UNPUBLISHED),
            (3, _FANTASYPROS_QB_PREVIOUS_SEASON),
        ):
            with self.subTest(week=week):
                task = PageCaptureTask(
                    "fantasypros",
                    2026,
                    week,
                    "visible_table",
                    fantasypros_projection_url(
                        "QB", week=week, horizon="weekly", scoring="PPR"
                    ),
                    projection=ProjectionTableSpec("weekly", "PPR", ("QB",)),
                )
                capture = self._capture(
                    task, body, wait_for_empty=week == 2
                )
                self.assertEqual(capture["availability"], "not_published")
                self.assertEqual(capture["tables"], [])

    def test_espn_accessible_custom_filters_are_selected_and_then_proven(self):
        task = PageCaptureTask(
            "espn",
            2026,
            1,
            "visible_table",
            "https://fantasy.espn.com/football/players/projections",
            projection=ProjectionTableSpec("weekly", "PPR", ("ALL",)),
        )
        page = self._browser.new_page(viewport={"width": 1600, "height": 1200})
        page.route(task.url, lambda route: route.fulfill(
            content_type="text/html", body=_ESPN_CUSTOM_FILTERS
        ))
        page.goto(task.url)
        self.addCleanup(page.close)
        request = {
            "provider": "espn",
            "season": 2026,
            "week": 1,
            "horizon": "weekly",
            "scoring": "PPR",
            "positions": ["ALL"],
        }
        changed = []
        for _ in range(13):
            result = page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request)
            if result["action"] == "ready":
                break
            self.assertEqual(result["action"], "changed")
            changed.append(result["dimension"])
        else:
            self.fail("ESPN custom filters did not reach a verified state")

        self.assertEqual(set(changed), {"season", "period", "scoring", "position"})
        capture = page.evaluate(PROJECTION_TABLE_SCRIPT, request)
        self.assertEqual(capture["availability"], "available")
        self.assertEqual(capture["source"], {
            "season": 2026,
            "week": 1,
            "horizon": "weekly",
            "scoring": "PPR",
            "positions": ["ALL"],
            "period_text": "2026 | Week 1 | PPR | ALL",
        })
        self.assertEqual(len(_typed_rows(task, capture)), 1)

    def test_duplicate_semantic_headers_are_rejected(self):
        task = _task("fantasypros", "QB")
        capture = self._capture(task, _DUPLICATE_FANTASYPROS)
        self.assertEqual(capture["tables"], [])

        raw = {
            "availability": "available",
            "source": capture["source"],
            "tables": [{
                "rows": [
                    [_cell("PLAYER"), _cell("PASS ATT"), _cell("PASS ATT"), _cell("FPTS")],
                    [
                        _cell(
                            "Jalen Hurts PHI",
                            ["https://www.fantasypros.com/nfl/projections/jalen-hurts.php"],
                        ),
                        _cell("34.2"),
                        _cell("8.1"),
                        _cell("24.1"),
                    ],
                ]
            }],
        }
        with self.assertRaisesRegex(BrowserCaptureError, "duplicate semantic headers"):
            projection_capture([raw], task)

    def test_visible_scoring_mismatch_is_not_relabelled_from_the_request(self):
        task = _task("fantasypros", "RB")
        capture = self._capture(task, _FANTASYPROS_RB_STANDARD)
        self.assertEqual(capture["source"]["scoring"], "STD")
        with self.assertRaisesRegex(BrowserCaptureError, "scoring"):
            projection_capture([capture], task)

    def test_fantasypros_runtime_url_must_match_every_requested_dimension(self):
        task = _task("fantasypros", "QB")
        page = self._browser.new_page(viewport={"width": 1600, "height": 1200})
        page.route(
            "https://www.fantasypros.com/**",
            lambda route: route.fulfill(content_type="text/html", body=_FANTASYPROS_QB),
        )
        page.goto(task.url + "&unexpected=1")
        self.addCleanup(page.close)
        request = {
            "provider": task.provider.value,
            "season": task.season,
            "week": task.week,
            "horizon": task.projection.horizon.value,
            "scoring": task.projection.scoring,
            "positions": list(task.projection.position_scope),
        }

        self.assertEqual(
            page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request),
            {"action": "error", "dimension": "fantasypros projection URL"},
        )
        self.assertIsNone(page.evaluate(PROJECTION_TABLE_SCRIPT, request))

    def _capture(self, task, body, *, wait_for_empty=False):
        page = self._browser.new_page(viewport={"width": 1600, "height": 1200})
        page.route(task.url, lambda route: route.fulfill(content_type="text/html", body=body))
        page.goto(task.url)
        self.addCleanup(page.close)
        request = {
            "provider": task.provider.value,
            "season": task.season,
            "week": task.week,
            "horizon": task.projection.horizon.value,
            "scoring": task.projection.scoring,
            "positions": list(task.projection.position_scope),
        }
        self.assertEqual(
            page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request),
            {"action": "ready"},
        )
        if wait_for_empty:
            self.assertEqual(
                page.evaluate(PROJECTION_TABLE_SCRIPT, request)["availability"],
                "unavailable",
            )
            page.wait_for_timeout(1600)
        return page.evaluate(PROJECTION_TABLE_SCRIPT, request)


class ProjectionHeaderContractTests(unittest.TestCase):
    def test_extension_embeds_the_same_projection_reader(self):
        reader = Path(
            "trade_snapshot/browser_extension/collectors/projection_read.js"
        ).read_text(encoding="utf-8")
        configurator = Path(
            "trade_snapshot/browser_extension/collectors/projection_configure.js"
        ).read_text(encoding="utf-8")
        self.assertIn(f"const readProjection = {PROJECTION_PAGE_SCRIPT.strip()};", reader)
        self.assertIn(
            f"const configureProjection = {CONFIGURE_PROJECTION_SCRIPT.strip()};",
            configurator,
        )

    def test_fantasypros_projection_identity_link_is_canonicalized(self):
        self.assertEqual(
            public_player_link(
                "fantasypros",
                "https://www.fantasypros.com/nfl/projections/jalen-hurts.php?week=1#news",
            ),
            "https://www.fantasypros.com/nfl/projections/jalen-hurts.php",
        )

    def test_fantasypros_projection_tasks_fail_closed_on_wrong_or_ros_dimensions(self):
        for url in (
            "https://www.fantasypros.com/nfl/projections/qb.php?week=1",
            "https://www.fantasypros.com/nfl/projections/rb.php?week=1&scoring=PPR",
            "https://www.fantasypros.com/nfl/projections/qb.php?week=2&scoring=PPR",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                ValueError, "exact position, period, and scoring"
            ):
                PageCaptureTask(
                    "fantasypros",
                    2026,
                    1,
                    "visible_table",
                    url,
                    projection=ProjectionTableSpec("weekly", "PPR", ("QB",)),
                )
        with self.assertRaisesRegex(ValueError, "dimensions are invalid"):
            fantasypros_projection_url("QB", week=1, horizon="ros", scoring="PPR")


def _task(provider, position):
    urls = {
        "fantasypros": (
            f"https://www.fantasypros.com/nfl/projections/{position.casefold()}.php"
            "?week=1&scoring=PPR"
        ),
        "espn": "https://fantasy.espn.com/football/players/projections",
        "yahoo": "https://football.fantasysports.yahoo.com/f1/players",
    }
    return PageCaptureTask(
        provider,
        2026,
        1,
        "visible_table",
        urls[provider],
        projection=ProjectionTableSpec("weekly", "PPR", (position,)),
    )


def _headers(capture):
    return tuple(cell["text"] for cell in capture["tables"][0]["rows"][0])


def _typed_rows(task, capture):
    data = projection_capture([capture], task)
    artifact = GenericTableArtifact(
        task.task_id,
        task.provider,
        task.season,
        task.week,
        task.kind,
        CAPTURED_AT,
        task.projection.horizon,
        task.projection.scoring,
        task.projection.position_scope,
        data.source_period_text,
        data.segments_captured,
        True,
        data.tables,
    )
    return projection_artifact_rows(artifact)


def _cell(text, links=None):
    return {"text": text, "links": links or []}


def _launch_browser(playwright, test_case):
    for options in (
        {"channel": "chromium", "headless": True},
        {"channel": "msedge", "headless": True},
        {"executable_path": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "headless": True},
        {"headless": True},
    ):
        try:
            return playwright.chromium.launch(**options)
        except Exception:
            continue
    test_case.skipTest("No Playwright-compatible Chromium or Edge browser is installed")


_FANTASYPROS_QB = """<!doctype html><html><body>
<title>Week 1 QB Projections - Consensus Fantasy Football Stats for Quarterbacks | FantasyPros</title>
<h1>Fantasy Football Projections - Week 1</h1><h2>Consensus last updated Sep 2, 2026</h2>
<table id="data"><thead>
  <tr><td>&nbsp;</td><td colspan="5">Passing</td>
      <td colspan="3">Rushing</td><td colspan="2">Misc</td></tr>
  <tr><th>Player</th><th>Att</th><th>Cmp</th><th>Yds</th><th>TDs</th><th>Ints</th>
      <th>Att</th><th>Yds</th><th>TDs</th><th>FL</th><th>FPTS</th></tr>
</thead><tbody><tr>
  <td><a href="https://www.fantasypros.com/nfl/projections/jalen-hurts.php?week=1">Jalen Hurts</a> PHI</td>
  <td>34.2</td><td>22.8</td><td>258.4</td><td>1.8</td><td>0.7</td>
  <td>8.1</td><td>42.3</td><td>0.5</td><td>0.2</td><td>24.1</td>
</tr></tbody></table></body></html>"""


_FANTASYPROS_QB_UNPUBLISHED = """<!doctype html><html><body>
<title>Week 2 QB Projections - Consensus Fantasy Football Stats for Quarterbacks | FantasyPros</title>
<h1>Fantasy Football Projections - Week 2</h1>
<table id="data"><thead>
  <tr><td>&nbsp;</td><td colspan="5">Passing</td><td colspan="3">Rushing</td>
      <td colspan="2">Misc</td></tr>
  <tr><th>Player</th><th>Att</th><th>Cmp</th><th>Yds</th><th>TDs</th><th>Ints</th>
      <th>Att</th><th>Yds</th><th>TDs</th><th>FL</th><th>FPTS</th></tr>
</thead><tbody></tbody></table></body></html>"""


_FANTASYPROS_QB_PREVIOUS_SEASON = """<!doctype html><html><body>
<title>Week 3 QB Projections - Consensus Fantasy Football Stats for Quarterbacks | FantasyPros</title>
<h1>Fantasy Football Projections - 2025 Week 3</h1>
<h2>Consensus last updated Sep 22, 2025</h2>
<table id="data"><thead><tr><th>Player</th><th>FPTS</th></tr></thead><tbody><tr>
  <td><a href="/nfl/projections/jalen-hurts.php?week=3">Jalen Hurts</a> PHI</td><td>23.1</td>
</tr></tbody></table></body></html>"""


_ESPN_RB = """<!doctype html><html><body>
<h1>2026 Week 1 PPR RB Projections</h1>
<table class="Table"><thead>
  <tr><th rowspan="3">Player</th><th rowspan="3">Team</th><th rowspan="3">Pos</th><th rowspan="3">Status</th>
      <th colspan="3">Rushing</th><th colspan="2">Receiving</th><th rowspan="3">FPTS</th></tr>
  <tr><th colspan="2">Volume</th><th rowspan="2">TD</th>
      <th rowspan="2">Rec</th><th rowspan="2">TD</th></tr>
  <tr><th>Att</th><th>Yds</th></tr>
</thead><tbody><tr>
  <td><a href="https://www.espn.com/nfl/player/_/id/3929630/saquon-barkley">Saquon Barkley</a></td>
  <td>PHI</td><td>RB</td><td>Questionable</td><td>19.4</td><td>91.7</td><td>0.8</td>
  <td>3.2</td><td>0.2</td><td>20.4</td>
</tr></tbody></table></body></html>"""


_ESPN_CUSTOM_FILTERS = """<!doctype html><html><head>
<title>Fantasy Football Projections - ESPN</title></head><body>
<h1>Fantasy Football Projections</h1>
<div><span>Season</span><button role="combobox" aria-label="Season" aria-controls="season-list"
  aria-expanded="false" aria-valuetext="2025">2025</button>
  <div id="season-list" role="listbox" data-control="Season" hidden>
    <button role="option">2025</button><button role="option">2026</button></div></div>
<div><span>Scoring Period</span><button role="combobox" aria-label="Scoring Period"
  aria-controls="period-list" aria-expanded="false" aria-valuetext="Week 18">Week 18</button>
  <div id="period-list" role="listbox" hidden>
    <button role="option">Week 1</button><button role="option">Week 18</button></div></div>
<div><span>Scoring Type</span><button role="combobox" aria-label="Scoring Type"
  aria-controls="scoring-list" aria-expanded="false" aria-valuetext="Points Non-PPR">Points Non-PPR</button>
  <div id="scoring-list" role="listbox" hidden>
    <button role="option">Points Non-PPR</button><button role="option">Points PPR</button></div></div>
<div><span>Position</span><button role="combobox" aria-label="Position" aria-controls="position-list"
  aria-expanded="false" aria-valuetext="RB">RB</button>
  <div id="position-list" role="listbox" hidden>
    <button role="option">All</button><button role="option">RB</button></div></div>
<table class="Table"><thead><tr>
  <th>Player</th><th>Team</th><th>Pos</th><th>FPTS</th>
</tr></thead><tbody><tr>
  <td><a href="https://www.espn.com/nfl/player/_/id/3929630/saquon-barkley">Saquon Barkley</a></td>
  <td>PHI</td><td>RB</td><td>20.4</td>
</tr></tbody></table>
<script>
for (const control of document.querySelectorAll('[role="combobox"]')) {
  control.addEventListener('click', () => {
    const list = document.getElementById(control.getAttribute('aria-controls'));
    const open = control.getAttribute('aria-expanded') !== 'true';
    control.setAttribute('aria-expanded', String(open)); list.hidden = !open;
  });
}
for (const option of document.querySelectorAll('[role="option"]')) {
  option.addEventListener('click', (event) => {
    event.stopPropagation();
    const list = option.closest('[role="listbox"]');
    const control = document.querySelector(`[aria-controls="${list.id}"]`);
    control.setAttribute('aria-valuetext', option.innerText);
    control.setAttribute('aria-expanded', 'false'); control.innerText = option.innerText;
    list.hidden = true;
  });
}
</script></body></html>"""


_YAHOO_WR = """<!doctype html><html><body>
<select id="statusselect" name="status"><option value="ALL" selected>All Players</option></select>
<select id="statselect" name="stat1">
  <option value="S_PS_2026">2026 Projected Stats</option>
  <option value="S_PW_1" selected>Week 1</option>
</select>
<input name="pos" type="radio" value="WR" checked>
<table class="Table"><thead>
  <tr><th rowspan="2">Offense</th><th rowspan="2">Bye</th><th rowspan="2">Fan Pts</th>
      <th colspan="2">Rushing</th><th colspan="4">Receiving</th></tr>
  <tr><th>Car</th><th>Yards</th><th>Tgts</th><th>Rec</th><th>Yards</th><th>TDs</th></tr>
</thead><tbody><tr>
  <td><a class="name" data-ys-playerid="32687"
      href="https://sports.yahoo.com/nfl/players/32687/">Amon-Ra St. Brown</a>
      <span>Det - WR</span></td>
  <td>8</td><td>16.7</td><td>0.4</td><td>2.1</td><td>9.2</td><td>6.3</td><td>84.6</td><td>0.6</td>
</tr></tbody></table></body></html>"""


_DUPLICATE_FANTASYPROS = """<!doctype html><html><body>
<h1>2026 Week 1 PPR QB Projections</h1>
<table id="data"><thead><tr>
  <th>Player</th><th>Pass Att</th><th>Pass Att</th><th>FPTS</th>
</tr></thead><tbody><tr>
  <td><a href="https://www.fantasypros.com/nfl/projections/jalen-hurts.php">Jalen Hurts PHI</a></td>
  <td>34.2</td><td>8.1</td><td>24.1</td>
</tr></tbody></table></body></html>"""


_FANTASYPROS_RB_STANDARD = """<!doctype html><html><body>
<title>Week 1 Standard RB Projections - Consensus Fantasy Football Stats for Running Backs | FantasyPros</title>
<h1>Fantasy Football Projections - Week 1</h1><h2>Consensus last updated Sep 2, 2026</h2>
<table id="data"><thead><tr><th>Player</th><th>FPTS</th></tr></thead><tbody><tr>
  <td><a href="https://www.fantasypros.com/nfl/projections/jahmyr-gibbs.php?week=1">Jahmyr Gibbs</a> DET</td>
  <td>19.6</td>
</tr></tbody></table></body></html>"""


_FANTASYPROS_K = """<!doctype html><html><body>
<title>Week 1 K Projections - Consensus Fantasy Football Stats for Kickers | FantasyPros</title>
<h1>Fantasy Football Projections - Week 1</h1><h2>Consensus last updated Sep 2, 2026</h2>
<table id="data"><thead><tr>
  <th>Player</th><th>FG</th><th>FGA</th><th>XPT</th><th>FPTS</th>
</tr></thead><tbody><tr>
  <td><a href="/nfl/projections/cameron-dicker.php?week=1">Cameron Dicker</a> LAC</td>
  <td>1.9</td><td>2.3</td><td>2.8</td><td>8.6</td>
</tr></tbody></table></body></html>"""


if __name__ == "__main__":
    unittest.main()
