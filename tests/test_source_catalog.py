import unittest

from trade_snapshot.source_catalog import weekly_source_catalog
from trade_snapshot.weekly_collection import WeeklyCollectionRequest


class SourceCatalogTests(unittest.TestCase):
    def test_broad_catalog_discloses_automatic_sources_and_reference_only_aggregates(self):
        catalog = weekly_source_catalog(
            WeeklyCollectionRequest(
                2026,
                1,
                "PPR",
                host_league_url=(
                    "https://fantasy.espn.com/football/league?leagueId=12345"
                ),
                yahoo_projection_league_url=(
                    "https://football.fantasysports.yahoo.com/f1/456/players"
                ),
                use_fantasypros=True,
                use_broad_consensus=True,
            )
        )
        calculation = {
            row["provider"]: row for row in catalog["calculation_sources"]
        }
        references = {row["provider"]: row for row in catalog["reference_sources"]}
        profiles = {row["provider"]: row for row in catalog["profile_sources"]}

        self.assertEqual(catalog["projection_mode"], "broad_consensus")
        self.assertEqual(
            catalog["weekly_projection_preview"],
            {
                "scope": "current_week_only",
                "weeks": [1],
                "league_end_discovered_during_scan": False,
            },
        )
        self.assertEqual(calculation["FantasyPros"]["status"], "required")
        self.assertIn("excluded", calculation["FantasyPros"]["note"])
        self.assertEqual(calculation["ESPN"]["status"], "required")
        self.assertEqual(calculation["Yahoo"]["status"], "required")
        self.assertEqual(
            calculation["Yahoo"]["urls"],
            [
                "https://football.fantasysports.yahoo.com/"
                "f1/456/players?status=ALL"
            ],
        )
        self.assertIn("calculation input", calculation["Yahoo"]["note"])
        for provider in ("CBS Sports", "FFToday", "FantasySharks"):
            self.assertEqual(calculation[provider]["status"], "best_effort")
            self.assertTrue(calculation[provider]["urls"])
        self.assertEqual(references["FFA accuracy study"]["status"], "reference")
        self.assertIn("not counted", references["FFA accuracy study"]["note"])
        self.assertNotIn("Yahoo public pre-ranks", references)
        self.assertEqual(profiles["nflverse"]["status"], "best_effort")
        self.assertEqual(len(profiles["nflverse"]["urls"]), 5)
        self.assertTrue(any("stats_player_week_2026" in url for url in profiles["nflverse"]["urls"]))
        self.assertTrue(any("injuries_2024" in url for url in profiles["nflverse"]["urls"]))
        self.assertEqual(profiles["Sleeper"]["status"], "best_effort")
        self.assertEqual(len(profiles["Sleeper"]["urls"]), 3)
        self.assertIn("attribution", profiles["Sleeper"]["note"].casefold())
        self.assertEqual(profiles["DynastyProcess"]["status"], "best_effort")
        self.assertEqual(len(profiles["DynastyProcess"]["urls"]), 1)
        self.assertIn("db_playerids.csv", profiles["DynastyProcess"]["urls"][0])
        self.assertIn("gpl-3.0", profiles["DynastyProcess"]["note"].casefold())

    def test_independent_core_catalog_never_requires_fantasypros(self):
        catalog = weekly_source_catalog(
            WeeklyCollectionRequest(
                2026,
                1,
                "PPR",
                host_league_url=(
                    "https://fantasy.espn.com/football/league?leagueId=12345"
                ),
                yahoo_projection_league_url=(
                    "https://football.fantasysports.yahoo.com/f1/456/players"
                ),
                use_fantasypros=False,
                use_broad_consensus=False,
            )
        )
        calculation = {
            row["provider"]: row for row in catalog["calculation_sources"]
        }

        self.assertEqual(catalog["mode"], "independent")
        self.assertEqual(catalog["projection_mode"], "core_ensemble")
        self.assertEqual(calculation["FantasyPros"]["status"], "off")
        self.assertEqual(calculation["ESPN"]["status"], "required")
        self.assertEqual(calculation["Yahoo"]["status"], "required")
        self.assertIn("current-season", calculation["ESPN"]["note"])
        for provider in ("CBS Sports", "FFToday", "FantasySharks"):
            self.assertEqual(calculation[provider]["status"], "off")
        self.assertEqual(
            {row["provider"] for row in catalog["profile_sources"]},
            {"nflverse", "Sleeper", "DynastyProcess"},
        )

    def test_future_week_preview_covers_every_remaining_nfl_week(self):
        catalog = weekly_source_catalog(
            WeeklyCollectionRequest(
                2026,
                15,
                "PPR",
                host_league_url=(
                    "https://fantasy.espn.com/football/league?leagueId=12345"
                ),
                include_future_weekly=True,
                use_fantasypros=False,
                use_broad_consensus=True,
            )
        )

        self.assertEqual(
            catalog["weekly_projection_preview"],
            {
                "scope": "remaining_nfl_weeks",
                "weeks": [15, 16, 17, 18],
                "league_end_discovered_during_scan": True,
            },
        )
        calculation = {
            row["provider"]: row for row in catalog["calculation_sources"]
        }
        for provider in ("ESPN", "FFToday", "FantasySharks"):
            self.assertTrue(calculation[provider]["urls"])


if __name__ == "__main__":
    unittest.main()
