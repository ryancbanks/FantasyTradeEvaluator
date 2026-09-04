import unittest

from trade_snapshot.draft_preseason_projection import (
    PRESEASON_PROJECTION_FEATURE_NAMES,
    build_preseason_projection,
)


class DraftPreseasonProjectionTests(unittest.TestCase):
    def test_projects_prior_per_game_stats_without_identity_or_future_fields(self):
        features = build_preseason_projection(
            {
                1: {
                    "attempts": 30,
                    "passing_yards": 250,
                    "passing_tds": 2,
                    "interceptions": 1,
                    "rushing_yards": 20,
                },
                2: {
                    "attempts": 20,
                    "passing_yards": 150,
                    "passing_tds": 1,
                    "interceptions": 0,
                    "rushing_yards": 10,
                },
            },
            projected_games=16,
        )

        self.assertEqual(set(features), set(PRESEASON_PROJECTION_FEATURE_NAMES))
        self.assertEqual(features["projected_games"], 16)
        self.assertEqual(features["projected_stat.attempts"], 400)
        self.assertEqual(features["projected_stat.passing_yards"], 3_200)
        self.assertEqual(features["projected_fantasy_points"], 232)
        self.assertEqual(features["projected_fantasy_points_per_game"], 14.5)
        self.assertTrue(
            set(features).isdisjoint(
                {
                    "player_id",
                    "display_name",
                    "nfl_team_id",
                    "actual_points",
                    "future_points",
                }
            )
        )

    def test_missing_prior_season_stays_explicit_instead_of_using_hindsight(self):
        features = build_preseason_projection({}, projected_games=17)

        self.assertEqual(features["projected_games"], 17)
        self.assertIsNone(features["projected_fantasy_points"])
        self.assertIsNone(features["projected_fantasy_points_per_game"])
        self.assertTrue(
            all(
                value is None
                for name, value in features.items()
                if name != "projected_games"
            )
        )

    def test_rejects_invalid_projection_horizon(self):
        for value in (0, 26, 16.0, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "projected_games"
            ):
                build_preseason_projection({}, projected_games=value)


if __name__ == "__main__":
    unittest.main()
