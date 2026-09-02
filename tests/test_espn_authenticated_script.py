import json
from pathlib import Path
import unittest

from trade_snapshot.espn_payload_projection import (
    project_espn_league_payload,
    project_espn_pro_team_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    PROJECT_ROOT
    / "trade_snapshot"
    / "browser_extension"
    / "collectors"
    / "espn_main.js"
).read_text(encoding="utf-8")
ESPN_PAGE = "https://fantasy.espn.com/football/players/projections#fte-scan-v1"


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


class AuthenticatedEspnScriptTests(unittest.TestCase):
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

    def test_private_member_and_owner_fields_never_cross_extension_boundary(self):
        league = {
            "id": 77,
            "seasonId": 2026,
            "scoringPeriodId": 2,
            "members": [{"displayName": "PRIVATE MEMBER"}],
            "status": {"currentMatchupPeriod": 2, "finalScoringPeriod": 3},
            "settings": {
                "rosterSettings": {"lineupSlotCounts": {"0": 1, "21": 1}},
                "scheduleSettings": {
                    "divisions": [{"id": 1, "name": "One", "size": 1}],
                    "matchupPeriodCount": 2,
                    "playoffTeamCount": 1,
                    "playoffReseed": False,
                    "playoffSeedingRule": "TOTAL_POINTS_SCORED",
                },
                "scoringSettings": {
                    "allowOutOfPositionScoring": False,
                    "homeTeamBonus": 0,
                    "matchupTieRule": "NONE",
                    "matchupTieRuleBy": 0,
                    "playerRankType": "PPR",
                    "playoffHomeTeamBonus": 0,
                    "playoffMatchupTieRule": "NONE",
                    "playoffMatchupTieRuleBy": 0,
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{
                        "statId": 53,
                        "points": 1,
                        "pointsOverrides": {"1": 0.5},
                        "privateNote": "PRIVATE SCORING NOTE",
                    }],
                },
            },
            "teams": [{
                "id": 1,
                "name": "Alpha",
                "abbrev": "ALP",
                "divisionId": 1,
                "owners": ["PRIVATE OWNER"],
                "record": {"overall": {
                    "wins": 1,
                    "losses": 0,
                    "ties": 0,
                    "pointsFor": 100,
                    "pointsAgainst": 90,
                    "privateRecord": "PRIVATE RECORD",
                }},
                "roster": {"entries": [{
                    "playerId": 101,
                    "lineupSlotId": 0,
                    "privateEntry": "PRIVATE ENTRY",
                    "playerPoolEntry": {"player": {
                        "id": 101,
                        "fullName": "Player One",
                        "defaultPositionId": 1,
                        "proTeamId": 1,
                        "eligibleSlots": [0, 20, 21],
                        "privatePlayer": "PRIVATE PLAYER",
                    }},
                }]},
            }],
            "schedule": [{
                "id": 1,
                "matchupPeriodId": 2,
                "winner": "UNDECIDED",
                "home": {"teamId": 1, "totalPoints": 0, "private": "PRIVATE HOME"},
                "away": {"teamId": 2, "totalPoints": 0, "private": "PRIVATE AWAY"},
            }],
            "privateTopLevel": "PRIVATE TOP LEVEL",
        }
        pro_teams = {
            "settings": {
                "proTeams": [{
                    "id": 1,
                    "abbrev": "ARI",
                    "byeWeek": 8,
                    "location": "Arizona",
                    "name": "Cardinals",
                    "universeId": 1,
                    "proGamesByScoringPeriod": {
                        "1": [{
                            "id": 1001,
                            "scoringPeriodId": 1,
                            "awayProTeamId": 1,
                            "homeProTeamId": 2,
                            "date": 1788000000000,
                            "startTimeTBD": False,
                            "statsOfficial": False,
                            "validForLocking": True,
                            "privateGameData": "PRIVATE GAME",
                        }]
                    },
                    "teamPlayersByPosition": {"1": ["PRIVATE PLAYER ID"]},
                    "privateTeamData": "PRIVATE PRO TEAM",
                }],
                "privateSettings": "PRIVATE SETTINGS",
            },
            "privateTopLevel": "PRIVATE PRO ROOT",
        }
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.route(
            "https://fantasy.espn.com/**",
            lambda route: route.fulfill(content_type="text/html", body="<main></main>"),
        )

        def api(route):
            body = pro_teams if "proTeamSchedules_wl" in route.request.url else league
            route.fulfill(content_type="application/json", body=json.dumps(body))

        page.route("https://lm-api-reads.fantasy.espn.com/**", api)
        page.goto(ESPN_PAGE)
        page.evaluate("globalThis.__FTE_MAIN_HANDLERS = {}")
        page.evaluate(SCRIPT)

        captured = page.evaluate(
            """async (options) =>
              await globalThis.__FTE_MAIN_HANDLERS['espn.authenticated_json'](options)
            """,
            {
                "season": 2026,
                "league_id": "77",
                "timeout_ms": 5000,
                "maximum_bytes": 1024 * 1024,
            },
        )

        self.assertEqual(captured["league"], project_espn_league_payload(league))
        self.assertEqual(
            captured["pro_teams"], project_espn_pro_team_payload(pro_teams)
        )
        serialized = json.dumps(captured, sort_keys=True)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("members", captured["league"])
        self.assertNotIn("owners", captured["league"]["teams"][0])
        self.assertEqual(
            captured["league"]["teams"][0]["roster"]["entries"][0][
                "playerPoolEntry"
            ]["player"]["fullName"],
            "Player One",
        )
        self.assertEqual(
            captured["league"]["settings"]["scoringSettings"]["scoringItems"],
            [{"statId": 53, "points": 1, "pointsOverrides": {"1": 0.5}}],
        )
        projected_pro_team = captured["pro_teams"]["settings"]["proTeams"][0]
        self.assertEqual(projected_pro_team["id"], 1)
        self.assertEqual(projected_pro_team["abbrev"], "ARI")
        self.assertEqual(projected_pro_team["teamPlayersByPosition"], {})
        self.assertEqual(
            projected_pro_team["proGamesByScoringPeriod"]["1"][0]["id"],
            1001,
        )


if __name__ == "__main__":
    unittest.main()
