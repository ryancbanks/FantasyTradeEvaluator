import json
import unittest

from tests.capture_fixtures import league_sources
from trade_snapshot._production_source_policy import espn_host_id
from trade_snapshot._league_script import LEAGUE_SOURCE_SCRIPT
from trade_snapshot.capture_schema import LeagueSource, LeagueSourceKind
from trade_snapshot.weekly_collection import WeeklyCollectionError


class EspnLeagueLinkFallbackTests(unittest.TestCase):
    def test_pasted_league_home_supplies_id_when_fantasypros_omits_its_link(self):
        configured = "https://fantasy.espn.com/football/league?leagueId=123"
        for metadata in ({"host": "ESPN"}, {}):
            with self.subTest(metadata=metadata):
                self.assertEqual(espn_host_id(metadata, configured), "123")

        with self.assertRaisesRegex(WeeklyCollectionError, "not linked to ESPN"):
            espn_host_id({"host": "YAHOO"}, configured)

    def test_matching_ids_are_accepted_and_mismatches_fail_closed(self):
        metadata = {"host": "ESPN", "host_league_id": "123"}
        self.assertEqual(
            espn_host_id(
                metadata,
                "https://fantasy.espn.com/football/league?leagueId=123",
            ),
            "123",
        )
        with self.assertRaisesRegex(WeeklyCollectionError, "does not match"):
            espn_host_id(
                metadata,
                "https://fantasy.espn.com/football/league?leagueId=456",
            )

    def test_missing_both_ids_gives_the_user_an_actionable_message(self):
        with self.assertRaisesRegex(WeeklyCollectionError, "League Home"):
            espn_host_id({"host": "ESPN"}, None)

    def test_bootstrap_allows_configured_id_fallback_but_checks_present_ids(self):
        bootstrap = league_sources()[0].to_record()["body"]
        bootstrap["payload"]["league"]["host"] = "ESPN"
        source = LeagueSource(LeagueSourceKind.BOOTSTRAP, bootstrap)
        self.assertNotIn(
            "host_league_id", source.to_record()["body"]["payload"]["league"]
        )

        bootstrap["payload"]["league"]["host_league_id"] = "not-a-number"
        with self.assertRaises(ValueError):
            LeagueSource(LeagueSourceKind.BOOTSTRAP, bootstrap)

    def test_fantasypros_nested_espn_link_is_recognized_when_present(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional for source-only test runs")
        league = {
            "key": "runtime-only-key",
            "season": 2026,
            "playoffsTeams": 1,
            "rosterSize": 14,
            "scoring": "PPR",
            "host": "ESPN",
            "url": (
                "https://fantasy.espn.com/football/league/transactions"
                "?LeagueID=123&seasonId=2026"
            ),
        }
        page_data = {
            "league": league,
            "teams": [
                {"teamId": 1, "teamName": "One", "players": [{"player_id": 101}]},
                {"teamId": 2, "teamName": "Two", "players": [{"player_id": 102}]},
            ],
            "playerInfo": {
                "101": {"player_id": 101, "player_name": "A"},
                "102": {"player_id": 102, "player_name": "B"},
            },
        }
        current = {
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
                    "teamId": team_id,
                    "teamName": name,
                    "rank_proj": rank,
                    "rank_current": rank,
                    "wins_current": wins,
                    "losses_current": losses,
                    "wins_proj": projected_wins,
                    "losses_proj": 14 - projected_wins,
                    "playoffs_odds": odds,
                    "championship_odds": odds / 2,
                }
                for team_id, name, rank, wins, losses, projected_wins, odds in (
                    (1, "One", 1, 1, 0, 8, 60),
                    (2, "Two", 2, 0, 1, 6, 40),
                )
            ],
        }
        body = """<!doctype html><script>
          const data=Object.freeze(%s);
          window.__tradeSnapshotAnalyzerV2={initQueue:[%s],error:null};
          window.MPB={getProjectedStandings:(_args,ok)=>ok(%s)};
        </script>""" % tuple(map(json.dumps, (page_data, current, projected)))
        with sync_playwright() as playwright:
            browser = None
            for options in (
                {"channel": "chromium", "headless": True},
                {"channel": "msedge", "headless": True},
                {"headless": True},
            ):
                try:
                    browser = playwright.chromium.launch(**options)
                    break
                except Exception:
                    continue
            if browser is None:
                self.skipTest("No Playwright-compatible Chromium or Edge is installed")
            try:
                page = browser.new_page()
                analyzer = (
                    "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php"
                )
                page.route(
                    analyzer,
                    lambda route: route.fulfill(content_type="text/html", body=body),
                )
                page.goto(analyzer)
                captured = page.evaluate(
                    LEAGUE_SOURCE_SCRIPT,
                    {
                        "timeout_ms": 5000,
                        "expected_season": 2026,
                        "expected_week": 1,
                    },
                )
            finally:
                browser.close()
        bootstrap = next(
            row for row in captured["sources"] if row["source"] == "bootstrap"
        )
        self.assertEqual(
            bootstrap["body"]["payload"]["league"]["host_league_id"], "123"
        )
        self.assertNotIn("runtime-only-key", json.dumps(captured))


if __name__ == "__main__":
    unittest.main()
