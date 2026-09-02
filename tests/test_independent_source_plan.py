import unittest

from trade_snapshot.capture_schema import CaptureKind, CaptureProvider, RankingHorizon
from trade_snapshot.independent_source_plan import (
    build_independent_weekly_source_plan,
)


class IndependentSourcePlanTests(unittest.TestCase):
    def test_plan_contains_only_deterministic_espn_yahoo_projection_tasks(self):
        first = build_independent_weekly_source_plan(
            season=2026,
            as_of_week=4,
            remaining_weeks=(6, 4, 5),
            scoring="PPR",
            player_positions=("WR", "RB", "IDP"),
        )
        reordered = build_independent_weekly_source_plan(
            season=2026,
            as_of_week=4,
            remaining_weeks=(5, 6, 4),
            scoring="PPR",
            player_positions=("DB", "LB", "RB", "WR", "DL"),
        )

        self.assertEqual(first, reordered)
        self.assertEqual(first.plan_id, reordered.plan_id)
        self.assertEqual(
            [
                (task.provider, task.week, task.projection.horizon)
                for task in first.tasks
            ],
            [
                (provider, week, RankingHorizon.WEEKLY)
                for week in (4, 5, 6)
                for provider in (CaptureProvider.ESPN, CaptureProvider.YAHOO)
            ]
            + [
                (CaptureProvider.ESPN, 4, RankingHorizon.ROS),
                (CaptureProvider.YAHOO, 4, RankingHorizon.ROS),
            ],
        )
        self.assertTrue(
            all(task.kind is CaptureKind.VISIBLE_TABLE for task in first.tasks)
        )
        self.assertTrue(
            all(task.projection.position_scope == ("ALL",) for task in first.tasks)
        )
        self.assertEqual(
            {task.url for task in first.tasks},
            {
                "https://fantasy.espn.com/football/players/projections",
                "https://football.fantasysports.yahoo.com/f1/players",
            },
        )
        self.assertNotIn("fantasypros", str(first.to_record()).casefold())

    def test_current_week_only_still_captures_weekly_and_ros_from_both_sources(self):
        plan = build_independent_weekly_source_plan(
            season=2026,
            as_of_week=8,
            remaining_weeks=(8, 9, 10),
            scoring="HALF",
            player_positions=("QB",),
            include_future_weekly=False,
        )

        self.assertEqual(len(plan.tasks), 4)
        self.assertEqual({task.week for task in plan.tasks}, {8})
        self.assertEqual(
            {task.projection.horizon for task in plan.tasks},
            {RankingHorizon.WEEKLY, RankingHorizon.ROS},
        )
        self.assertEqual(
            {task.provider for task in plan.tasks},
            {CaptureProvider.ESPN, CaptureProvider.YAHOO},
        )

    def test_rejects_invalid_dimensions_before_publishing_a_plan(self):
        valid = {
            "season": 2026,
            "as_of_week": 4,
            "remaining_weeks": (4, 5),
            "scoring": "PPR",
            "player_positions": ("RB",),
        }
        invalid = (
            {"season": 1999},
            {"as_of_week": 0},
            {"remaining_weeks": ()},
            {"remaining_weeks": (4, 4)},
            {"remaining_weeks": (4, [])},
            {"remaining_weeks": (3, 4)},
            {"remaining_weeks": "4"},
            {"scoring": "custom"},
            {"player_positions": ()},
            {"player_positions": ("PUNTER",)},
            {"player_positions": "RB"},
            {"include_future_weekly": 1},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_independent_weekly_source_plan(**(valid | changes))


if __name__ == "__main__":
    unittest.main()
