import unittest

from trade_snapshot.capture_schema import CaptureProvider, RankingHorizon
from trade_snapshot.public_projection_plan import public_projection_tasks


class PublicProjectionPlanTests(unittest.TestCase):
    def test_weekly_plan_uses_supported_publishers_and_queryless_bootstraps(self):
        tasks = public_projection_tasks(
            season=2026,
            week=7,
            horizon="weekly",
            scoring="PPR",
            positions=("DB", "DL", "DST", "K", "LB", "RB", "QB", "RB"),
        )

        self.assertEqual(
            tuple((task.provider, task.projection.position_scope) for task in tasks),
            (
                (CaptureProvider.FFTODAY, ("K",)),
                (CaptureProvider.FFTODAY, ("QB",)),
                (CaptureProvider.FFTODAY, ("RB",)),
                (CaptureProvider.FANTASYSHARKS, ("DB",)),
                (CaptureProvider.FANTASYSHARKS, ("DL",)),
                (CaptureProvider.FANTASYSHARKS, ("DST",)),
                (CaptureProvider.FANTASYSHARKS, ("K",)),
                (CaptureProvider.FANTASYSHARKS, ("LB",)),
                (CaptureProvider.FANTASYSHARKS, ("QB",)),
                (CaptureProvider.FANTASYSHARKS, ("RB",)),
            ),
        )
        self.assertTrue(all("?" not in task.url for task in tasks))
        self.assertTrue(
            all(task.projection.horizon is RankingHorizon.WEEKLY for task in tasks)
        )

    def test_ros_plan_adds_cbs_and_complete_public_position_support(self):
        tasks = public_projection_tasks(
            season=2026,
            week=7,
            horizon="ros",
            scoring="HALF",
            positions=("DB", "DST", "RB"),
        )

        by_provider = {}
        for task in tasks:
            by_provider.setdefault(task.provider, set()).add(
                task.projection.position_scope[0]
            )
        self.assertEqual(by_provider[CaptureProvider.CBS], {"DST", "RB"})
        self.assertEqual(
            by_provider[CaptureProvider.FFTODAY], {"DB", "RB"}
        )
        self.assertEqual(
            by_provider[CaptureProvider.FANTASYSHARKS], {"DB", "DST", "RB"}
        )
        cbs_urls = {
            task.url for task in tasks if task.provider is CaptureProvider.CBS
        }
        self.assertTrue(all(url.endswith("/ppr/") for url in cbs_urls))
        self.assertTrue(all("?" not in task.url for task in tasks))

    def test_rejects_empty_or_invalid_dimensions_through_task_schema(self):
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            public_projection_tasks(
                season=2026,
                week=1,
                horizon="weekly",
                scoring="PPR",
                positions=(),
            )
        for change in (
            {"season": 1999},
            {"week": 0},
            {"horizon": "season"},
            {"scoring": "custom"},
        ):
            values = {
                "season": 2026,
                "week": 1,
                "horizon": "weekly",
                "scoring": "PPR",
                "positions": ("RB",),
            }
            with self.subTest(change=change), self.assertRaises(ValueError):
                public_projection_tasks(**(values | change))

    def test_task_schema_rejects_unjoinable_fftoday_surfaces(self):
        from trade_snapshot.capture_schema import PageCaptureTask, ProjectionTableSpec

        cases = (
            ("weekly", "DL", "https://www.fftoday.com/rankings/playerwkproj.php"),
            ("weekly", "DST", "https://www.fftoday.com/rankings/playerwkproj.php"),
            ("ros", "DST", "https://www.fftoday.com/rankings/playerproj.php"),
            ("weekly", "QB", "https://www.fftoday.com/rankings/playerproj.php"),
        )
        for horizon, position, url in cases:
            with self.subTest(horizon=horizon, position=position), self.assertRaisesRegex(
                ValueError, "unsupported period or position"
            ):
                PageCaptureTask(
                    "fftoday",
                    2026,
                    1,
                    "visible_table",
                    url,
                    projection=ProjectionTableSpec(horizon, "PPR", (position,)),
                )


if __name__ == "__main__":
    unittest.main()
