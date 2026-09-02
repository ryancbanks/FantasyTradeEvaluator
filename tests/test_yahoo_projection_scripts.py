import unittest

from trade_snapshot._capture_scripts import (
    ADVANCE_PROJECTION_SCRIPT,
    CONFIGURE_PROJECTION_SCRIPT,
    PROJECTION_TABLE_SCRIPT,
)
from trade_snapshot._projection_tables import projection_capture
from trade_snapshot.capture_schema import PageCaptureTask, ProjectionTableSpec


YAHOO_PAGE = "https://football.fantasysports.yahoo.com/2026/f1/12345/playersearch"


def yahoo_task(*, horizon="weekly", week=1, scoring="HALF", position="RB"):
    return PageCaptureTask(
        "yahoo",
        2026,
        week,
        "visible_table",
        "https://football.fantasysports.yahoo.com/f1/players",
        projection=ProjectionTableSpec(horizon, scoring, (position,)),
    )


def yahoo_player_list(*, next_links=(), misleading_period=False):
    period_options = (
        "<option value='S_PW_2'>Week 1</option>"
        if misleading_period
        else "<option value='S_PW_1'>Week 1</option>"
    )
    navigation = "".join(
        "<div id='playerspagenav%s'><ul><li class='last'>"
        "<a class='next' href='%s' onclick='event.preventDefault(); "
        "window.nextClicks=(window.nextClicks||0)+1'>Next 25</a>"
        "</li></ul></div>" % (index, href)
        for index, href in enumerate(next_links, 1)
    )
    return f"""<!doctype html><html><body><main>
      <select id="statusselect" name="status">
        <option value="A" selected>All Available Players</option>
        <option value="ALL">All Players</option>
      </select>
      <select id="statselect" name="stat1">
        <option value="ADVST" selected>Actual Stats</option>
        {period_options}
        <option value="S_PS_2026">2026 Projected Stats</option>
        <option value="S_PSR_2026">Rest of Season</option>
      </select>
      <label><input type="radio" name="pos" value="O" checked>Offense</label>
      <label><input type="radio" name="pos" value="RB">RB</label>
      <label><input type="radio" name="pos" value="DEF">Defense</label>
      <table id="players-table" class="Table">
        <thead>
          <tr>
            <th>Offense</th><th>Bye</th><th>Fan Pts</th>
            <th colspan="3">Passing</th><th colspan="3">Rushing</th>
          </tr>
          <tr>
            <th>Offense</th><th>Bye</th><th>Fan Pts</th>
            <th>Yds</th><th>TD</th><th>Int</th>
            <th>Att</th><th>Yds</th><th>TD</th>
          </tr>
        </thead>
        <tbody><tr>
          <td>
            <a href="https://sports.yahoo.com/nfl/players/31818/news">News</a>
            <a class="name" data-ys-playerid="31818"
               href="https://sports.yahoo.com/nfl/players/31818">Jahmyr Gibbs</a>
            <span>Det - RB</span><span>Player note text</span>
          </td>
          <td>8</td><td>18.45</td><td>0</td><td>0</td><td>0</td>
          <td>15</td><td>82</td><td>1</td>
        </tr></tbody>
      </table>
      {navigation}
    </main></body></html>"""


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


class YahooProjectionScriptTests(unittest.TestCase):
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

    def open_page(self, body, *, query=""):
        page = self._browser.new_page()
        page.route("https://football.fantasysports.yahoo.com/**", lambda route: route.fulfill(
            content_type="text/html", body=body,
        ))
        page.goto(YAHOO_PAGE + query)
        self.addCleanup(page.close)
        return page

    def test_exact_controls_then_grouped_table_produce_typed_projection(self):
        page = self.open_page(yahoo_player_list())
        request = {
            "provider": "yahoo",
            "season": 2026,
            "week": 1,
            "horizon": "weekly",
            "scoring": "HALF",
            "positions": ["RB"],
        }

        actions = [page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request) for _ in range(4)]
        self.assertEqual(actions, [
            {"action": "changed", "dimension": "availability"},
            {"action": "changed", "dimension": "period"},
            {"action": "changed", "dimension": "position"},
            {"action": "ready"},
        ])
        self.assertEqual(page.locator("#statusselect").input_value(), "ALL")
        self.assertEqual(page.locator("#statselect").input_value(), "S_PW_1")
        self.assertEqual(page.locator('input[name="pos"]:checked').input_value(), "RB")

        captured = page.evaluate(PROJECTION_TABLE_SCRIPT, request)
        self.assertEqual(captured["source"], {
            "season": 2026,
            "week": 1,
            "horizon": "weekly",
            "scoring": "HALF",
            "positions": ["RB"],
            "period_text": "2026 | Week 1 | HALF | RB | Yahoo All Players",
        })
        rows = captured["tables"][0]["rows"]
        self.assertEqual([cell["text"] for cell in rows[0]], [
            "PLAYER", "BYE", "FAN PTS", "PASS YDS", "PASS TD", "PASS INT",
            "RUSH ATT", "RUSH YDS", "RUSH TD",
        ])
        self.assertEqual(rows[1][0], {
            "text": "Jahmyr Gibbs Det - RB",
            "links": ["https://sports.yahoo.com/nfl/players/31818"],
        })
        parsed = projection_capture([captured], yahoo_task())
        self.assertEqual(parsed.segments_captured, 1)
        self.assertEqual(parsed.tables[0].rows[1][0].text, "Jahmyr Gibbs Det - RB")

    def test_control_text_cannot_substitute_for_exact_yahoo_period_value(self):
        page = self.open_page(yahoo_player_list(misleading_period=True))
        request = {
            "provider": "yahoo",
            "season": 2026,
            "week": 1,
            "horizon": "weekly",
            "scoring": "HALF",
            "positions": ["RB"],
        }
        self.assertEqual(page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request), {
            "action": "error", "dimension": "yahoo period",
        })

    def test_duplicate_next_links_accept_playersearch_to_players_alias(self):
        destination = (
            "https://football.fantasysports.yahoo.com/2026/f1/12345/players"
            "?status=ALL&stat1=S_PW_1&pos=RB&count=25"
        )
        page = self.open_page(
            yahoo_player_list(next_links=(destination, destination)),
            query="?status=ALL&stat1=S_PW_1&pos=RB&count=0",
        )
        self.assertEqual(page.evaluate(ADVANCE_PROJECTION_SCRIPT, "yahoo"), {
            "action": "next",
        })
        self.assertEqual(page.evaluate("window.nextClicks"), 1)

    def test_conflicting_next_links_fail_closed_without_clicking(self):
        base = "https://football.fantasysports.yahoo.com/2026/f1/12345/players"
        first = base + "?status=ALL&stat1=S_PW_1&pos=RB&count=25"
        second = base + "?status=ALL&stat1=S_PW_1&pos=RB&count=50"
        page = self.open_page(
            yahoo_player_list(next_links=(first, second)),
            query="?status=ALL&stat1=S_PW_1&pos=RB&count=0",
        )
        self.assertEqual(page.evaluate(ADVANCE_PROJECTION_SCRIPT, "yahoo"), {
            "action": "error",
        })
        self.assertIsNone(page.evaluate("window.nextClicks || null"))


if __name__ == "__main__":
    unittest.main()
