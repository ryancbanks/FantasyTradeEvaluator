from datetime import datetime, timezone
import unittest

from tests.test_weekly_engine import state
from trade_snapshot.nfl_schedule import NflSchedule, NflTeamWeek, NflTeamWeekStatus
from trade_snapshot.projection_schedule import materialize_weekly_grid
from trade_snapshot.projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def weekly():
    return WeeklyProjection(
        "p1",
        "snapshot-1",
        "profile-1",
        "espn",
        "espn-p1",
        2026,
        1,
        ProjectionStatus.OBSERVED,
        NOW,
        10,
        {"points": 10, "yards": 100},
        "NFL-A",
        "G1",
        "NFL-B",
        True,
    )


def ros(*, applicable=(1, 2), status=ProjectionStatus.OBSERVED):
    return RemainingSeasonProjection(
        "p1",
        "snapshot-1",
        "profile-1",
        "espn",
        "espn-p1",
        2026,
        applicable,
        status,
        RemainingSeasonOrigin.PROVIDER_PUBLISHED,
        NOW,
        30 if status is ProjectionStatus.OBSERVED else None,
        {"points": 30, "yards": 300} if status is ProjectionStatus.OBSERVED else {},
    )


def schedule(*, week2_bye=False):
    rows = [
        NflTeamWeek(
            "NFL-A", 1, NflTeamWeekStatus.SCHEDULED, "G1", "NFL-B", True
        ),
        NflTeamWeek(
            "NFL-B", 1, NflTeamWeekStatus.SCHEDULED, "G1", "NFL-A", False
        ),
    ]
    if week2_bye:
        rows.extend(
            (
                NflTeamWeek("NFL-A", 2, NflTeamWeekStatus.BYE),
                NflTeamWeek("NFL-B", 2, NflTeamWeekStatus.BYE),
            )
        )
    else:
        rows.extend(
            (
                NflTeamWeek(
                    "NFL-A", 2, NflTeamWeekStatus.SCHEDULED, "G2", "NFL-B", False
                ),
                NflTeamWeek(
                    "NFL-B", 2, NflTeamWeekStatus.SCHEDULED, "G2", "NFL-A", True
                ),
            )
        )
    return NflSchedule(2026, NOW, "espn", tuple(rows))


def materialize(rows, *, nfl_schedule=None):
    return materialize_weekly_grid(
        state(),
        rows,
        player_ids=("p1",),
        provider_names=("espn",),
        nfl_schedule=nfl_schedule or schedule(),
        player_nfl_team_ids={"p1": "NFL-A"},
    )


class ProjectionScheduleTests(unittest.TestCase):
    def test_allocates_only_unpublished_active_weeks_and_labels_origin(self):
        result = materialize((weekly(), ros()))
        first, second = result
        self.assertEqual(first.origin, WeeklyProjectionOrigin.PROVIDER_PUBLISHED)
        self.assertEqual(second.origin, WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON)
        self.assertEqual(second.projected_fantasy_points, 20)
        self.assertEqual(dict(second.raw_projected_stats), {"points": 20, "yards": 200})
        self.assertEqual(second.nfl_team_id, "NFL-A")
        self.assertEqual(second.nfl_game_id, "G2")
        self.assertEqual(second.opponent_team_id, "NFL-B")
        self.assertFalse(second.is_home)

    def test_marks_bye_and_missing_publication_without_fabricating_zero(self):
        bye_rows = materialize(
            (weekly(), ros(applicable=(1,))),
            nfl_schedule=schedule(week2_bye=True),
        )
        self.assertEqual(bye_rows[1].status, ProjectionStatus.BYE)
        self.assertIsNone(bye_rows[1].projected_fantasy_points)

        missing_rows = materialize((weekly(),))
        self.assertEqual(missing_rows[1].status, ProjectionStatus.NOT_PUBLISHED)
        self.assertIsNone(missing_rows[1].projected_fantasy_points)

    def test_ros_fills_an_explicit_not_published_week(self):
        placeholder = WeeklyProjection(
            "p1",
            "snapshot-1",
            "profile-1",
            "espn",
            "espn-p1",
            2026,
            2,
            ProjectionStatus.NOT_PUBLISHED,
            NOW,
            nfl_team_id="NFL-A",
            nfl_game_id="G2",
            opponent_team_id="NFL-B",
            is_home=False,
        )

        result = materialize((weekly(), placeholder, ros()))

        self.assertEqual(result[1].status, ProjectionStatus.OBSERVED)
        self.assertEqual(result[1].origin, WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON)
        self.assertEqual(result[1].projected_fantasy_points, 20)
        self.assertEqual(result[1].nfl_game_id, "G2")
        self.assertEqual(result[1].opponent_team_id, "NFL-B")
        self.assertFalse(result[1].is_home)

    def test_rejects_weekly_values_that_conflict_with_ros_schedule(self):
        with self.assertRaisesRegex(ValueError, "applicable weeks"):
            materialize((weekly(), ros(applicable=(2,))))

    def test_parse_error_prevents_unsafe_ros_redistribution(self):
        parse_error = WeeklyProjection(
            "p1",
            "snapshot-1",
            "profile-1",
            "espn",
            "espn-p1",
            2026,
            1,
            ProjectionStatus.PARSE_ERROR,
            NOW,
            nfl_team_id="NFL-A",
        )

        result = materialize((parse_error, ros()))

        self.assertEqual(result[0].status, ProjectionStatus.PARSE_ERROR)
        self.assertEqual(result[1].status, ProjectionStatus.NOT_PUBLISHED)
        self.assertIsNone(result[1].projected_fantasy_points)

    def test_rejects_conflicting_provider_identity(self):
        changed = WeeklyProjection(
            "p1",
            "snapshot-1",
            "profile-1",
            "espn",
            "other-id",
            2026,
            2,
            ProjectionStatus.OBSERVED,
            NOW,
            10,
            {},
            "NFL-A",
        )
        with self.assertRaisesRegex(ValueError, "conflicting provider IDs"):
            materialize((weekly(), changed))

    def test_rejects_provider_game_context_that_conflicts_with_verified_schedule(self):
        changed = WeeklyProjection(
            "p1",
            "snapshot-1",
            "profile-1",
            "espn",
            "espn-p1",
            2026,
            2,
            ProjectionStatus.OBSERVED,
            NOW,
            10,
            {},
            "NFL-A",
            "wrong-game",
            "NFL-B",
            False,
        )
        with self.assertRaisesRegex(ValueError, "verified NFL schedule"):
            materialize((weekly(), changed))


if __name__ == "__main__":
    unittest.main()
