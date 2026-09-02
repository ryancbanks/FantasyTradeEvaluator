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

        self.assertEqual(catalog["projection_mode"], "broad_consensus")
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
        self.assertIn("point projections", calculation["ESPN"]["note"])
        for provider in ("CBS Sports", "FFToday", "FantasySharks"):
            self.assertEqual(calculation[provider]["status"], "off")


if __name__ == "__main__":
    unittest.main()
