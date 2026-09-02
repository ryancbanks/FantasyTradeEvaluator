import unittest

from trade_snapshot.capture_schema import (
    CaptureKind,
    CapturePlan,
    FantasyProsECRTask,
    PageCaptureTask,
    RankingHorizon,
)
from trade_snapshot.source_plan import build_weekly_source_plan


class SourcePlanTests(unittest.TestCase):
    def test_builds_queryless_multiweek_plan_with_bounded_provider_pages(self):
        plan = build_weekly_source_plan(
            season=2026,
            as_of_week=1,
            remaining_weeks=(1, 2),
            scoring="PPR",
            player_positions=("RB", "S"),
        )
        self.assertEqual(CapturePlan.from_record(plan.to_record()), plan)
        self.assertEqual(len(plan.tasks), 31)
        self.assertEqual({task.week for task in plan.tasks}, {1, 2})
        self.assertFalse(any("?" in task.url for task in plan.tasks))
        ecr = tuple(task for task in plan.tasks if isinstance(task, FantasyProsECRTask))
        self.assertEqual(len(ecr), 4)
        self.assertEqual({task.position_scope for task in ecr}, {("RB",), ("DB",)})
        tables = tuple(
            task for task in plan.tasks
            if isinstance(task, PageCaptureTask)
            and task.kind is CaptureKind.VISIBLE_TABLE
        )
        espn = tuple(task for task in tables if task.provider.value == "espn")
        yahoo = tuple(task for task in tables if task.provider.value == "yahoo")
        public = tuple(
            task
            for task in tables
            if task.provider.value in {"cbs", "fftoday", "fantasysharks"}
        )
        self.assertTrue(all(task.projection.position_scope == ("ALL",) for task in espn))
        self.assertEqual(
            {task.projection.position_scope for task in yahoo},
            {("RB",), ("DB",)},
        )
        self.assertEqual(
            {task.provider.value for task in public},
            {"cbs", "fftoday", "fantasysharks"},
        )
        self.assertFalse(
            any(
                task.provider.value == "cbs"
                and task.projection.horizon is RankingHorizon.WEEKLY
                for task in public
            )
        )

    def test_can_limit_weekly_pages_to_current_week_but_keeps_ros(self):
        plan = build_weekly_source_plan(
            season=2026,
            as_of_week=3,
            remaining_weeks=(3, 4, 5),
            scoring="HALF",
            player_positions=("QB",),
            include_future_weekly=False,
        )
        self.assertEqual({task.week for task in plan.tasks}, {3})
        self.assertTrue(
            any(
                isinstance(task, FantasyProsECRTask)
                and task.url.endswith("half-point-ppr-qb.php")
                for task in plan.tasks
            )
        )
        ros = tuple(
            task for task in plan.tasks
            if isinstance(task, FantasyProsECRTask) and task.horizon.value == "ros"
        )
        self.assertEqual(len(ros), 1)
        self.assertTrue(ros[0].url.endswith("ros-half-point-ppr-qb.php"))
        projection_tasks = tuple(
            task
            for task in plan.tasks
            if isinstance(task, PageCaptureTask) and task.projection is not None
        )
        self.assertEqual(
            {task.provider.value for task in projection_tasks},
            {
                "fantasypros",
                "espn",
                "yahoo",
                "cbs",
                "fftoday",
                "fantasysharks",
            },
        )
        self.assertEqual(
            {task.projection.horizon for task in projection_tasks},
            {RankingHorizon.WEEKLY, RankingHorizon.ROS},
        )
        self.assertTrue(all(task.week == 3 for task in projection_tasks))

    def test_core_ensemble_keeps_espn_and_yahoo_when_broad_sources_are_off(self):
        plan = build_weekly_source_plan(
            season=2026,
            as_of_week=3,
            remaining_weeks=(3,),
            scoring="PPR",
            player_positions=("RB",),
            include_future_weekly=False,
            broad_consensus=False,
        )
        projection_tasks = tuple(
            task
            for task in plan.tasks
            if isinstance(task, PageCaptureTask) and task.projection is not None
        )

        self.assertEqual(
            {task.provider.value for task in projection_tasks},
            {"fantasypros", "espn", "yahoo"},
        )
        self.assertEqual(
            {task.projection.horizon for task in projection_tasks},
            {RankingHorizon.WEEKLY, RankingHorizon.ROS},
        )


if __name__ == "__main__":
    unittest.main()
