import unittest

from trade_snapshot._capture_scripts import PROJECTION_TABLE_SCRIPT
from trade_snapshot._projection_parse import projection_artifact_rows
from trade_snapshot._projection_tables import projection_capture
from trade_snapshot.capture_schema import (
    CaptureProvider,
    GenericTableArtifact,
    PageCaptureTask,
    ProjectionTableSpec,
    VisibleTable,
    VisibleTableCell,
    public_player_link,
)


YAHOO_TEAM_URL = "https://sports.yahoo.com/nfl/teams/houston/"


class YahooDefenseCaptureTests(unittest.TestCase):
    def test_live_shaped_grouped_defense_table_survives_strict_parser(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional for source-only test runs")

        with sync_playwright() as playwright:
            browser = _browser(playwright, self)
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1200})
                page.route(
                    "https://football.fantasysports.yahoo.com/**",
                    lambda route: route.fulfill(
                        content_type="text/html", body=_YAHOO_DST_HTML
                    ),
                )
                page.goto(
                    "https://football.fantasysports.yahoo.com/f1/101/players"
                    "?status=ALL&pos=DEF&stat1=S_PW_1"
                )
                captured = page.evaluate(
                    PROJECTION_TABLE_SCRIPT,
                    {
                        "provider": "yahoo",
                        "season": 2026,
                        "week": 1,
                        "horizon": "weekly",
                        "scoring": "HALF",
                        "positions": ["DST"],
                    },
                )
            finally:
                browser.close()

        task = _task("DST")
        data = projection_capture([captured], task)
        artifact = GenericTableArtifact(
            task.task_id,
            task.provider,
            task.season,
            task.week,
            task.kind,
            "2026-09-01T14:15:16Z",
            task.projection.horizon,
            task.projection.scoring,
            task.projection.position_scope,
            data.source_period_text,
            data.segments_captured,
            True,
            data.tables,
        )
        rows = projection_artifact_rows(artifact)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].identity_provider, "yahoo")
        self.assertEqual(rows[0].provider_player_id, "dst:HOU")
        self.assertEqual(rows[0].display_name, "Texans")
        self.assertEqual(rows[0].position, "DST")
        self.assertEqual(rows[0].nfl_team_id, "HOU")
        self.assertEqual(rows[0].projected_fantasy_points, 6.11)
        self.assertEqual(
            dict(rows[0].raw_projected_stats),
            {
                "def_sack": 2.4,
                "def_safe": 0.0,
                "def_int": 0.7,
                "def_fum_rec": 0.4,
                "def_td": 0.2,
                "def_blk_kick": 0.1,
                "ret_td": 0.0,
            },
        )
        self.assertEqual(artifact.tables[0].rows[1][0].links, (YAHOO_TEAM_URL,))

    def test_yahoo_team_link_is_exact_and_rejected_for_a_non_defense_row(self):
        self.assertEqual(
            public_player_link("yahoo", YAHOO_TEAM_URL + "?guccounter=1#notes"),
            YAHOO_TEAM_URL,
        )
        for invalid in (
            "https://sports.yahoo.com/nfl/teams/",
            "https://sports.yahoo.com/nfl/teams/houston/news/",
            "https://sports.yahoo.com/nfl/teams/houston_texans/",
            "https://example.com/nfl/teams/houston/",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(public_player_link("yahoo", invalid))

        task = _task("RB")
        artifact = GenericTableArtifact(
            task.task_id,
            task.provider,
            task.season,
            task.week,
            task.kind,
            "2026-09-01T14:15:16Z",
            task.projection.horizon,
            task.projection.scoring,
            task.projection.position_scope,
            "2026 | Week 1 | HALF | RB | Yahoo All Players",
            1,
            True,
            (
                VisibleTable((
                    (VisibleTableCell("PLAYER"), VisibleTableCell("FAN PTS")),
                    (
                        VisibleTableCell("Texans HOU - RB", (YAHOO_TEAM_URL,)),
                        VisibleTableCell("6.11"),
                    ),
                )),
            ),
        )
        with self.assertRaisesRegex(ValueError, "only for an NFL team defense"):
            projection_artifact_rows(artifact)


def _task(position):
    return PageCaptureTask(
        "yahoo",
        2026,
        1,
        "visible_table",
        "https://football.fantasysports.yahoo.com/f1/players",
        projection=ProjectionTableSpec("weekly", "HALF", (position,)),
    )


def _browser(playwright, case):
    for channel in ("chromium", "msedge"):
        try:
            return playwright.chromium.launch(channel=channel, headless=True)
        except Exception:
            continue
    case.skipTest("A Playwright Chromium-family browser is not installed")


_YAHOO_DST_HTML = """<!doctype html><html><body>
<select id="statusselect" name="status"><option value="ALL" selected>All Players</option></select>
<select id="statselect" name="stat1">
  <option value="S_PS_2026">2026 Season (projected)</option>
  <option value="S_PW_1" selected>Week 1 (projected)</option>
</select>
<input name="pos" type="radio" value="DEF" checked>
<table class="Table"><thead>
<tr>
  <th colspan="3"></th><th></th><th></th><th></th><th>Fantasy</th>
  <th colspan="2">Rankings</th><th>Trends</th><th></th>
  <th colspan="2">Tackles</th><th colspan="2">Turnovers</th>
  <th>TD</th><th>Misc</th><th>Ret</th><th></th>
</tr>
<tr>
  <th></th><th></th><th>Defense/Special Teams</th><th>Roster Status</th>
  <th>GP*</th><th>Bye</th><th>Fan Pts</th><th>Pre-Season</th><th>Actual</th>
  <th>% Ros</th><th>Pts vs.*</th><th>Sack</th><th>Safe</th><th>Int</th>
  <th>Fum Rec</th><th>TD</th><th>Blk Kick</th><th>TD</th><th></th>
</tr></thead><tbody><tr>
  <td>+</td><td>*</td><td>
    <a class="name" data-ys-playerid="100034"
       href="https://sports.yahoo.com/nfl/teams/houston/">Texans</a>
    <span>Hou - DEF</span><span>Sun 1:00 pm vs Buf</span>
  </td>
  <td>Team 4</td><td>-</td><td>8</td><td>6.11</td><td>155</td><td>184</td>
  <td>99%</td><td>22.4</td><td>2.4</td><td>0.0</td><td>0.7</td><td>0.4</td>
  <td>0.2</td><td>0.1</td><td>0.0</td><td></td>
</tr></tbody></table>
</body></html>"""


if __name__ == "__main__":
    unittest.main()
