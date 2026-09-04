import unittest

from trade_snapshot.capture_schema import CaptureKind, CaptureProvider, RankingHorizon
from trade_snapshot.independent_source_plan import (
    build_independent_weekly_source_plan,
)


class IndependentSourcePlanTests(unittest.TestCase):
    def test_plan_contains_deterministic_espn_yahoo_and_public_projection_tasks(self):
        first = build_independent_weekly_source_plan(
            season=2026,
            as_of_week=4,
            remaining_weeks=(6, 4, 5),
            scoring="PPR",
            player_positions=("WR", "RB", "IDP"),
            include_future_weekly=True,
        )
        reordered = build_independent_weekly_source_plan(
            season=2026,
            as_of_week=4,
            remaining_weeks=(5, 6, 4),
            scoring="PPR",
            player_positions=("DB", "LB", "RB", "WR", "DL"),
            include_future_weekly=True,
        )

        self.assertEqual(first, reordered)
        self.assertEqual(first.plan_id, reordered.plan_id)
        self.assertEqual(len(first.tasks), 54)
        self.assertTrue(
            all(task.kind is CaptureKind.VISIBLE_TABLE for task in first.tasks)
        )
        self.assertTrue(
            all(
                task.projection.position_scope == ("ALL",)
                for task in first.tasks
                if task.provider is CaptureProvider.ESPN
            )
        )
        self.assertEqual(
            {
                task.projection.horizon
                for task in first.tasks
                if task.provider is CaptureProvider.ESPN
            },
            {RankingHorizon.ROS},
        )
        self.assertEqual(
            {task.provider for task in first.tasks},
            {
                CaptureProvider.ESPN,
                CaptureProvider.YAHOO,
                CaptureProvider.CBS,
                CaptureProvider.FFTODAY,
                CaptureProvider.FANTASYSHARKS,
            },
        )
        self.assertNotIn("fantasypros", str(first.to_record()).casefold())

    def test_current_week_only_keeps_weekly_and_ros_public_coverage(self):
        plan = build_independent_weekly_source_plan(
            season=2026,
            as_of_week=8,
            remaining_weeks=(8, 9, 10),
            scoring="HALF",
            player_positions=("QB",),
            include_future_weekly=False,
        )

        self.assertEqual(len(plan.tasks), 8)
        self.assertEqual({task.week for task in plan.tasks}, {8})
        self.assertEqual(
            {task.projection.horizon for task in plan.tasks},
            {RankingHorizon.WEEKLY, RankingHorizon.ROS},
        )
        self.assertEqual(
            {task.provider for task in plan.tasks},
            {
                CaptureProvider.ESPN,
                CaptureProvider.YAHOO,
                CaptureProvider.CBS,
                CaptureProvider.FFTODAY,
                CaptureProvider.FANTASYSHARKS,
            },
        )

    def test_core_fallback_visits_espn_and_yahoo(self):
        plan = build_independent_weekly_source_plan(
            season=2026,
            as_of_week=8,
            remaining_weeks=(8, 9),
            scoring="PPR",
            player_positions=("RB",),
            include_future_weekly=False,
            broad_consensus=False,
        )

        self.assertEqual(len(plan.tasks), 3)
        self.assertEqual(
            {task.provider for task in plan.tasks},
            {CaptureProvider.ESPN, CaptureProvider.YAHOO},
        )
        self.assertEqual(
            {task.projection.horizon for task in plan.tasks},
            {RankingHorizon.WEEKLY, RankingHorizon.ROS},
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
