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
    def test_builds_dimension_bound_multiweek_plan_with_bounded_provider_pages(self):
        plan = build_weekly_source_plan(
            season=2026,
            as_of_week=1,
            remaining_weeks=(1, 2),
            scoring="PPR",
            player_positions=("RB", "S"),
            include_future_weekly=True,
        )
        self.assertEqual(CapturePlan.from_record(plan.to_record()), plan)
        self.assertEqual(len(plan.tasks), 25)
        self.assertEqual({task.week for task in plan.tasks}, {1, 2})
        fantasypros_projections = tuple(
            task for task in plan.tasks
            if isinstance(task, PageCaptureTask)
            and task.kind is CaptureKind.VISIBLE_TABLE
            and task.provider.value == "fantasypros"
        )
        self.assertEqual(len(fantasypros_projections), 2)
        self.assertTrue(all(task.projection.horizon is RankingHorizon.WEEKLY for task in fantasypros_projections))
        self.assertEqual(
            {task.url for task in fantasypros_projections},
            {
                f"https://www.fantasypros.com/nfl/projections/{position}.php"
                f"?week={week}&scoring=PPR"
                for position in ("rb", "db") for week in (1,)
            },
        )
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

    def test_current_week_capture_plus_ros_avoids_unpublished_future_pages(self):
        plan = build_weekly_source_plan(
            season=2026,
            as_of_week=3,
            remaining_weeks=(3, 4, 5),
            scoring="HALF",
            player_positions=("QB",),
            include_future_weekly=False,
        )
        fantasypros_projections = tuple(
            task
            for task in plan.tasks
            if isinstance(task, PageCaptureTask)
            and task.kind is CaptureKind.VISIBLE_TABLE
            and task.provider.value == "fantasypros"
        )
        self.assertEqual(
            {(task.week, task.projection.horizon) for task in fantasypros_projections},
            {
                (3, RankingHorizon.WEEKLY),
            },
        )
        self.assertTrue(
            any(
                isinstance(task, FantasyProsECRTask)
                and task.url.endswith("/qb.php")
                for task in plan.tasks
            )
        )
        ros = tuple(
            task for task in plan.tasks
            if isinstance(task, FantasyProsECRTask) and task.horizon.value == "ros"
        )
        self.assertEqual(len(ros), 1)
        self.assertTrue(ros[0].url.endswith("/ros-qb.php"))
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
        optional_provider_tasks = tuple(
            task
            for task in projection_tasks
            if task.provider.value in {"espn", "yahoo"}
        )
        self.assertTrue(all(task.week == 3 for task in optional_provider_tasks))
        self.assertEqual(
            {
                (task.provider.value, task.week)
                for task in optional_provider_tasks
                if task.projection.horizon is RankingHorizon.WEEKLY
            },
            {("yahoo", 3)},
        )
        self.assertEqual(
            {
                (task.provider.value, task.week)
                for task in optional_provider_tasks
                if task.projection.horizon is RankingHorizon.ROS
            },
            {("espn", 3), ("yahoo", 3)},
        )

    def test_ecr_scoring_prefixes_apply_only_to_reception_positions(self):
        plan = build_weekly_source_plan(
            season=2026,
            as_of_week=1,
            remaining_weeks=(1,),
            scoring="PPR",
            player_positions=("QB", "RB", "WR", "TE", "K", "DST", "IDP"),
            include_future_weekly=False,
        )
        urls = {
            task.url
            for task in plan.tasks
            if isinstance(task, FantasyProsECRTask)
        }

        for position in ("rb", "wr", "te"):
            self.assertIn(
                f"https://www.fantasypros.com/nfl/rankings/ppr-{position}.php",
                urls,
            )
            self.assertIn(
                f"https://www.fantasypros.com/nfl/rankings/ros-ppr-{position}.php",
                urls,
            )
        reception_tasks = tuple(
            task
            for task in plan.tasks
            if isinstance(task, FantasyProsECRTask)
            and task.position_scope[0] in {"RB", "WR", "TE"}
        )
        self.assertTrue(all(task.source_scoring == "PPR" for task in reception_tasks))
        for position in ("qb", "k", "dst", "dl", "lb", "db"):
            self.assertIn(
                f"https://www.fantasypros.com/nfl/rankings/{position}.php",
                urls,
            )
            self.assertIn(
                f"https://www.fantasypros.com/nfl/rankings/ros-{position}.php",
                urls,
            )
            self.assertFalse(any(f"ppr-{position}.php" in url for url in urls))
        non_reception_tasks = tuple(
            task
            for task in plan.tasks
            if isinstance(task, FantasyProsECRTask)
            and task.position_scope[0] in {"QB", "K", "DST", "DL", "LB", "DB"}
        )
        self.assertTrue(all(task.scoring == "PPR" for task in non_reception_tasks))
        self.assertTrue(all(task.source_scoring == "STD" for task in non_reception_tasks))

    def test_fantasypros_projection_urls_encode_supported_weekly_scoring_only(self):
        expected_suffix = {
            "STD": "?week=4&scoring=STD",
            "HALF": "?week=4",
            "PPR": "?week=4&scoring=PPR",
        }
        for scoring, suffix in expected_suffix.items():
            with self.subTest(scoring=scoring):
                plan = build_weekly_source_plan(
                    season=2026,
                    as_of_week=4,
                    remaining_weeks=(4,),
                    scoring=scoring,
                    player_positions=("RB",),
                    include_future_weekly=False,
                )
                tasks = tuple(
                    task for task in plan.tasks
                    if isinstance(task, PageCaptureTask)
                    and task.kind is CaptureKind.VISIBLE_TABLE
                    and task.provider.value == "fantasypros"
                )
                self.assertEqual(len(tasks), 1)
                self.assertTrue(tasks[0].url.endswith(suffix))
                self.assertIs(tasks[0].projection.horizon, RankingHorizon.WEEKLY)

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
