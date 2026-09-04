from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from trade_snapshot.browser_capture import BrowserCaptureOptions, ProjectionNotPublished
from trade_snapshot.capture_schema import CaptureKind, CapturePlan, PageCaptureTask
from trade_snapshot.production_collection import _collect_remaining_sources
from trade_snapshot.projection_source import ProjectionAttemptStatus
from trade_snapshot.source_plan import build_weekly_source_plan


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _plan():
    complete = build_weekly_source_plan(
        season=2026,
        as_of_week=1,
        remaining_weeks=(1, 2, 3, 4),
        scoring="PPR",
        player_positions=("RB", "WR"),
        broad_consensus=False,
    )
    return CapturePlan(
        task for task in complete.tasks if task.kind is not CaptureKind.LEAGUE_SOURCE
    )


def _fantasypros_weekly_tasks(plan, week):
    return tuple(
        task
        for task in plan.tasks
        if isinstance(task, PageCaptureTask)
        and task.provider.value == "fantasypros"
        and task.projection.horizon.value == "weekly"
        and task.week == week
    )


def _projection_tasks(plan):
    return tuple(
        task
        for task in plan.tasks
        if isinstance(task, PageCaptureTask)
        and task.kind is CaptureKind.VISIBLE_TABLE
    )


class _Collector:
    def __init__(self, unpublished=()):
        self.unpublished = frozenset(task.task_id for task in unpublished)
        self.calls = []

    def collect(self, plan, _options, **_kwargs):
        task = plan.tasks[0]
        self.calls.append(task)
        if task.task_id in self.unpublished:
            raise ProjectionNotPublished("requested season is not published")
        return (SimpleNamespace(
            artifact_id=f"captable_{task.task_id.rsplit('_', 1)[1]}",
            captured_at="2026-09-01T00:00:00Z",
        ),)


class _Clock:
    def __init__(self):
        self.values = []

    def __call__(self):
        value = NOW + timedelta(seconds=len(self.values))
        self.values.append(value)
        return value


def _collect(plan, collector, *, clock=None, task_progress=None):
    return _collect_remaining_sources(
        collector,
        plan,
        BrowserCaptureOptions(Path("profile")),
        object(),
        SimpleNamespace(is_ready=lambda _task: True),
        {},
        first_remaining_week=1,
        attempt_clock=clock or (lambda: NOW),
        task_progress=task_progress,
    )


class FantasyProsProjectionCutoffTests(unittest.TestCase):
    def test_full_future_week_short_circuits_every_later_fantasypros_week(self):
        plan = _plan()
        week_two = _fantasypros_weekly_tasks(plan, 2)
        collector = _Collector(week_two)

        _artifacts, attempts = _collect(plan, collector)

        called_ids = {task.task_id for task in collector.calls}
        self.assertTrue(all(task.task_id in called_ids for task in week_two))
        for week in (3, 4):
            self.assertTrue(all(
                task.task_id not in called_ids
                for task in _fantasypros_weekly_tasks(plan, week)
            ))
        unpublished = {
            attempt.task_id
            for attempt in attempts
            if attempt.status is ProjectionAttemptStatus.NOT_PUBLISHED
        }
        self.assertEqual(
            unpublished,
            {
                task.task_id
                for week in (2, 3, 4)
                for task in _fantasypros_weekly_tasks(plan, week)
            },
        )

    def test_partial_future_week_never_establishes_a_cutoff(self):
        plan = _plan()
        week_two = _fantasypros_weekly_tasks(plan, 2)
        collector = _Collector((week_two[0],))

        _collect(plan, collector)

        called_ids = {task.task_id for task in collector.calls}
        for week in (2, 3, 4):
            self.assertTrue(all(
                task.task_id in called_ids
                for task in _fantasypros_weekly_tasks(plan, week)
            ))

    def test_current_week_not_published_retains_fail_closed_behavior(self):
        plan = _plan()
        current_week = _fantasypros_weekly_tasks(plan, 1)
        collector = _Collector(current_week)

        with self.assertRaises(ProjectionNotPublished):
            _collect(plan, collector)

        called_ids = [task.task_id for task in collector.calls]
        self.assertIn(current_week[0].task_id, called_ids)
        self.assertNotIn(current_week[1].task_id, called_ids)
        self.assertFalse(any(
            task.task_id in called_ids
            for week in (2, 3, 4)
            for task in _fantasypros_weekly_tasks(plan, week)
        ))

    def test_short_circuit_preserves_attempt_order_timestamps_and_other_sources(self):
        plan = _plan()
        collector = _Collector(_fantasypros_weekly_tasks(plan, 2))
        clock = _Clock()
        progress = []

        _artifacts, attempts = _collect(
            plan,
            collector,
            clock=clock,
            task_progress=lambda current, total, task: progress.append(
                (current, total, task.task_id)
            ),
        )

        projection_tasks = _projection_tasks(plan)
        self.assertEqual(
            [attempt.task_id for attempt in attempts],
            [task.task_id for task in projection_tasks],
        )
        self.assertEqual(
            progress,
            [
                (index, len(plan.tasks), task.task_id)
                for index, task in enumerate(plan.tasks, start=1)
            ],
        )
        unpublished_attempts = [
            attempt
            for attempt in attempts
            if attempt.status is ProjectionAttemptStatus.NOT_PUBLISHED
        ]
        self.assertEqual(
            [attempt.attempted_at for attempt in unpublished_attempts],
            clock.values,
        )
        self.assertEqual(len(clock.values), 6)
        called_ids = {task.task_id for task in collector.calls}
        self.assertTrue(all(
            task.task_id in called_ids
            for task in plan.tasks
            if task.provider.value != "fantasypros"
        ))
        self.assertTrue(all(
            task.task_id in called_ids
            for task in plan.tasks
            if task.kind is CaptureKind.ECR_RANKINGS
        ))


if __name__ == "__main__":
    unittest.main()
