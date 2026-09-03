from dataclasses import replace
from datetime import datetime, timezone
import unittest

from tests.test_weekly_engine import state
from trade_snapshot.nfl_schedule import NflSchedule, NflTeamWeek, NflTeamWeekStatus
from trade_snapshot.projection_schedule import (
    materialize_weekly_grid,
    normalize_ros_active_weeks,
    validate_weekly_projection_schedule,
)
from trade_snapshot.projections import (
    ProjectionStatus,
    ProviderStatusObservation,
    ProviderStatusScope,
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


def schedule(*, week2_bye=False, final_week=2):
    rows = []
    for week in range(1, final_week + 1):
        if week == 2 and week2_bye:
            rows.extend(
                (
                    NflTeamWeek("NFL-A", week, NflTeamWeekStatus.BYE),
                    NflTeamWeek("NFL-B", week, NflTeamWeekStatus.BYE),
                )
            )
            continue
        rows.extend(
            (
                NflTeamWeek(
                    "NFL-A",
                    week,
                    NflTeamWeekStatus.SCHEDULED,
                    f"G{week}",
                    "NFL-B",
                    week % 2 == 1,
                ),
                NflTeamWeek(
                    "NFL-B",
                    week,
                    NflTeamWeekStatus.SCHEDULED,
                    f"G{week}",
                    "NFL-A",
                    week % 2 == 0,
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
    def test_validates_retained_weekly_rows_beyond_the_calculation_window(self):
        future = WeeklyProjection(
            "p1",
            "snapshot-1",
            "profile-1",
            "espn",
            "espn-p1",
            2026,
            3,
            ProjectionStatus.OBSERVED,
            NOW,
            10,
            {"points": 10},
            "NFL-A",
        )
        base = schedule(final_week=18)
        rows = tuple(row for row in base.team_weeks if row.week != 3) + (
            NflTeamWeek("NFL-A", 3, NflTeamWeekStatus.BYE),
            NflTeamWeek("NFL-B", 3, NflTeamWeekStatus.BYE),
        )

        with self.assertRaisesRegex(ValueError, "conflicts with an NFL bye"):
            validate_weekly_projection_schedule(
                NflSchedule(2026, NOW, "espn", rows),
                {"p1": "NFL-A"},
                (future,),
            )

    def test_preserves_not_applicable_ros_evidence_without_inventing_a_scope(self):
        unavailable = ros(applicable=(), status=ProjectionStatus.NOT_APPLICABLE)

        self.assertIs(
            normalize_ros_active_weeks(
                unavailable,
                nfl_team_id="NFL-A",
                nfl_schedule=schedule(),
            ),
            unavailable,
        )

    def test_allocates_only_unpublished_active_weeks_and_labels_origin(self):
        source = ros()
        source = replace(
            source,
            provider_status_observations=(
                ProviderStatusObservation(
                    "Questionable",
                    NOW,
                    ProviderStatusScope.REST_OF_SEASON,
                ),
            ),
        )
        result = materialize((weekly(), source))
        first, second = result
        self.assertEqual(first.origin, WeeklyProjectionOrigin.PROVIDER_PUBLISHED)
        self.assertEqual(second.origin, WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON)
        self.assertEqual(second.projected_fantasy_points, 20)
        self.assertEqual(dict(second.raw_projected_stats), {"points": 20, "yards": 200})
        self.assertEqual(second.nfl_team_id, "NFL-A")
        self.assertEqual(second.nfl_game_id, "G2")
        self.assertEqual(second.opponent_team_id, "NFL-B")
        self.assertFalse(second.is_home)
        self.assertEqual(
            second.provider_status_observations,
            source.provider_status_observations,
        )
        self.assertIs(
            second.provider_status_observations[0].source_scope,
            ProviderStatusScope.REST_OF_SEASON,
        )

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
            provider_status_observations=(
                ProviderStatusObservation(
                    "Out",
                    NOW,
                    ProviderStatusScope.WEEKLY,
                    2,
                ),
            ),
        )
        ros_row = replace(
            ros(),
            provider_status_observations=(
                ProviderStatusObservation(
                    "Questionable",
                    NOW,
                    ProviderStatusScope.REST_OF_SEASON,
                ),
            ),
        )

        result = materialize((weekly(), placeholder, ros_row))

        self.assertEqual(result[1].status, ProjectionStatus.OBSERVED)
        self.assertEqual(result[1].origin, WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON)
        self.assertEqual(result[1].projected_fantasy_points, 20)
        self.assertEqual(result[1].nfl_game_id, "G2")
        self.assertEqual(result[1].opponent_team_id, "NFL-B")
        self.assertFalse(result[1].is_home)
        self.assertEqual(
            {
                (row.designation, row.source_scope, row.source_week)
                for row in result[1].provider_status_observations
            },
            {
                ("Out", ProviderStatusScope.WEEKLY, 2),
                ("Questionable", ProviderStatusScope.REST_OF_SEASON, None),
            },
        )

    def test_ros_is_allocated_across_its_full_nfl_horizon(self):
        result = materialize(
            (weekly(), ros(applicable=(1, 2, 3, 4))),
            nfl_schedule=schedule(final_week=4),
        )

        self.assertEqual(result[1].projected_fantasy_points, 20 / 3)

    def test_future_published_week_is_subtracted_before_ros_allocation(self):
        future = WeeklyProjection(
            "p1",
            "snapshot-1",
            "profile-1",
            "espn",
            "espn-p1",
            2026,
            3,
            ProjectionStatus.OBSERVED,
            NOW,
            12,
            {"points": 12, "yards": 120},
            "NFL-A",
            "G3",
            "NFL-B",
            True,
        )

        result = materialize(
            (weekly(), future, ros(applicable=(1, 2, 3, 4))),
            nfl_schedule=schedule(final_week=4),
        )

        self.assertEqual(result[1].projected_fantasy_points, 4)
        self.assertEqual(dict(result[1].raw_projected_stats), {"points": 4, "yards": 40})

    def test_ros_does_not_treat_an_absent_weekly_stat_as_zero(self):
        points_only = WeeklyProjection(
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
            {"points": 10},
            "NFL-A",
            "G1",
            "NFL-B",
            True,
        )

        result = materialize((points_only, ros()))

        self.assertEqual(dict(result[1].raw_projected_stats), {"points": 20})

    def test_rejects_ros_points_smaller_than_published_weekly_subtotal(self):
        too_small = RemainingSeasonProjection(
            "p1", "snapshot-1", "profile-1", "espn", "espn-p1", 2026,
            (1, 2), ProjectionStatus.OBSERVED,
            RemainingSeasonOrigin.PROVIDER_PUBLISHED, NOW, 9, {"points": 30},
        )
        with self.assertRaisesRegex(ValueError, "not coherent"):
            materialize((weekly(), too_small))

    def test_rejects_ros_stat_smaller_than_published_weekly_subtotal(self):
        too_small = RemainingSeasonProjection(
            "p1", "snapshot-1", "profile-1", "espn", "espn-p1", 2026,
            (1, 2), ProjectionStatus.OBSERVED,
            RemainingSeasonOrigin.PROVIDER_PUBLISHED, NOW, 30,
            {"points": 30, "yards": 99},
        )
        with self.assertRaisesRegex(ValueError, "not coherent"):
            materialize((weekly(), too_small))

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
