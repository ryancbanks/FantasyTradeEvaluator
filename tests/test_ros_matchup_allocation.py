from dataclasses import replace
from datetime import datetime, timezone
from math import fsum
import unittest

from trade_snapshot.league_state import (
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)
from trade_snapshot.nfl_schedule import NflSchedule, NflTeamWeek, NflTeamWeekStatus
from trade_snapshot.projection_schedule import materialize_weekly_grid
from trade_snapshot.projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)
from trade_snapshot.public_player_data import (
    DataAvailability,
    PlayerWeekStats,
    SeasonPlayerStats,
)
from trade_snapshot.ros_matchup_allocation import (
    ROS_MATCHUP_ALLOCATION_LIMITATION,
    ROS_MATCHUP_ALLOCATION_METHOD_ID,
    build_ros_matchup_allocation,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _stat(season, week, opponent, points, *, game_id=None, position="RB"):
    return PlayerWeekStats(
        f"{position}-{season}-{week}-{opponent}",
        "Historical Player",
        position,
        season,
        week,
        game_id or f"{season}-{week}-{opponent}",
        "KC",
        opponent,
        None,
        points,
        points,
        (),
    )


def _history_rows(season=2025):
    return tuple(
        _stat(season, week, "BUF", 5.0)
        for week in range(1, 5)
    ) + tuple(
        _stat(season, week, "DEN", 15.0)
        for week in range(5, 9)
    )


def _allocation(*, as_of_week=1, current_rows=()):
    return build_ros_matchup_allocation(
        season=2026,
        as_of_week=as_of_week,
        scoring="PPR",
        scoring_profile_id="profile-1",
        current_stats=SeasonPlayerStats(
            2026,
            DataAvailability.OBSERVED,
            current_rows,
        ),
        previous_stats=SeasonPlayerStats(
            2025,
            DataAvailability.OBSERVED,
            _history_rows(),
        ),
        source_data_id="public-player-data-test",
    )


def _state():
    return LeagueState(
        "snapshot-1",
        2026,
        "profile-1",
        1,
        (LeagueTeam("a", "Alpha"), LeagueTeam("b", "Bravo")),
        (
            TeamStanding("a", 0, 0, 0, 0, 0),
            TeamStanding("b", 0, 0, 0, 0, 0),
        ),
        tuple(FantasyMatchup(week, "a", "b") for week in range(1, 4)),
        RosterRules(1, ("RB",)),
        PlayoffRules(
            1,
            3,
            (4,),
            False,
            0,
            (Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def _game(week, away, home):
    game_id = f"G{week}-{away}-{home}"
    return (
        NflTeamWeek(
            away,
            week,
            NflTeamWeekStatus.SCHEDULED,
            game_id,
            home,
            False,
        ),
        NflTeamWeek(
            home,
            week,
            NflTeamWeekStatus.SCHEDULED,
            game_id,
            away,
            True,
        ),
    )


def _schedule(*, with_bye=False):
    rows = []
    rows.extend(_game(1, "KC", "LV" if not with_bye else "BUF"))
    rows.extend(_game(1, "BUF" if not with_bye else "LV", "DEN"))
    if with_bye:
        rows.extend(
            (
                NflTeamWeek("KC", 2, NflTeamWeekStatus.BYE),
                NflTeamWeek("LV", 2, NflTeamWeekStatus.BYE),
            )
        )
        rows.extend(_game(2, "BUF", "DEN"))
    else:
        rows.extend(_game(2, "KC", "BUF"))
        rows.extend(_game(2, "LV", "DEN"))
    rows.extend(_game(3, "KC", "DEN"))
    rows.extend(_game(3, "LV", "BUF"))
    return NflSchedule(2026, NOW, "espn", tuple(rows))


def _ros():
    return RemainingSeasonProjection(
        "p1",
        "snapshot-1",
        "profile-1",
        "espn",
        "espn-p1",
        2026,
        (1, 2, 3),
        ProjectionStatus.OBSERVED,
        RemainingSeasonOrigin.PROVIDER_PUBLISHED,
        NOW,
        50.0,
        {"yards": 500.0},
    )


def _published_week_one():
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
        10.0,
        {"yards": 100.0},
        "KC",
        "G1-KC-LV",
        "LV",
        False,
    )


def _materialize(rows, *, nfl_schedule=None, allocation=None):
    return materialize_weekly_grid(
        _state(),
        rows,
        player_ids=("p1",),
        provider_names=("espn",),
        nfl_schedule=nfl_schedule or _schedule(),
        player_nfl_team_ids={"p1": "KC"},
        player_positions={"p1": "RB"},
        ros_matchup_allocation=allocation or _allocation(),
    )


class RosMatchupAllocationTests(unittest.TestCase):
    def test_uses_completed_position_opponent_games_and_exposes_limits(self):
        allocation = _allocation()

        self.assertLess(allocation.factor("RB", "BUF"), 1.0)
        self.assertGreater(allocation.factor("RB", "DEN"), 1.0)
        self.assertEqual(allocation.factor("RB", "LV"), 1.0)
        self.assertEqual(allocation.source_seasons, (2025,))
        self.assertEqual(allocation.method_id, ROS_MATCHUP_ALLOCATION_METHOD_ID)
        self.assertIn("provider-published", ROS_MATCHUP_ALLOCATION_LIMITATION)

    def test_sparse_and_unavailable_samples_fall_back_to_neutral(self):
        sparse = build_ros_matchup_allocation(
            season=2026,
            as_of_week=1,
            scoring="PPR",
            scoring_profile_id="profile-1",
            current_stats=SeasonPlayerStats(
                2026, DataAvailability.NOT_PUBLISHED, ()
            ),
            previous_stats=SeasonPlayerStats(
                2025,
                DataAvailability.OBSERVED,
                tuple(_stat(2025, week, "LV", 10.0) for week in range(1, 4)),
            ),
            source_data_id="public-player-data-sparse",
        )

        self.assertEqual(sparse.factor("RB", "LV"), 1.0)
        self.assertEqual(sparse.factor("DST", "LV"), 1.0)
        self.assertEqual(sparse.weights, ())
        materialized = _materialize(
            (_published_week_one(), _ros()),
            allocation=sparse,
        )
        self.assertEqual(
            tuple(row.projected_fantasy_points for row in materialized),
            (10.0, 20.0, 20.0),
        )

    def test_partial_source_failure_reports_only_the_contributing_season(self):
        allocation = build_ros_matchup_allocation(
            season=2026,
            as_of_week=9,
            scoring="PPR",
            scoring_profile_id="profile-1",
            current_stats=SeasonPlayerStats(
                2026, DataAvailability.UNAVAILABLE, ()
            ),
            previous_stats=SeasonPlayerStats(
                2025, DataAvailability.OBSERVED, _history_rows()
            ),
            source_data_id="public-player-data-partial",
        )

        self.assertEqual(allocation.source_seasons, (2025,))
        self.assertIn("Contributing seasons: 2025", allocation.provenance)
        self.assertNotIn("2026", allocation.provenance)

    def test_target_season_rows_at_or_after_as_of_week_cannot_change_weights(self):
        completed = (_stat(2026, 2, "BUF", 7.0),)
        base = _allocation(as_of_week=3, current_rows=completed)
        future = _allocation(
            as_of_week=3,
            current_rows=(
                *completed,
                _stat(2026, 3, "BUF", 10_000.0),
                _stat(2026, 4, "DEN", -10_000.0),
            ),
        )

        self.assertEqual(future, base)

    def test_exact_season_pair_and_supported_scoring_are_required(self):
        with self.assertRaisesRegex(ValueError, "current_stats season"):
            build_ros_matchup_allocation(
                season=2025,
                as_of_week=1,
                scoring="PPR",
                scoring_profile_id="profile-1",
                current_stats=SeasonPlayerStats(
                    2026, DataAvailability.OBSERVED, ()
                ),
                previous_stats=SeasonPlayerStats(
                    2025, DataAvailability.OBSERVED, ()
                ),
                source_data_id="public-player-data-test",
            )
        with self.assertRaisesRegex(ValueError, "scoring"):
            build_ros_matchup_allocation(
                season=2026,
                as_of_week=1,
                scoring="custom",
                scoring_profile_id="profile-1",
                current_stats=SeasonPlayerStats(
                    2026, DataAvailability.OBSERVED, ()
                ),
                previous_stats=SeasonPlayerStats(
                    2025, DataAvailability.OBSERVED, ()
                ),
                source_data_id="public-player-data-test",
            )

    def test_weighted_residual_conserves_ros_totals_and_published_week(self):
        rows = _materialize((_published_week_one(), _ros()))

        self.assertEqual(rows[0], _published_week_one())
        self.assertEqual(rows[0].origin, WeeklyProjectionOrigin.PROVIDER_PUBLISHED)
        self.assertLess(
            rows[1].projected_fantasy_points,
            rows[2].projected_fantasy_points,
        )
        self.assertEqual(
            fsum(row.projected_fantasy_points for row in rows),
            _ros().projected_fantasy_points,
        )
        self.assertEqual(
            fsum(dict(row.raw_projected_stats)["yards"] for row in rows),
            dict(_ros().raw_projected_stats)["yards"],
        )
        self.assertTrue(
            all(
                row.origin is WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON
                for row in rows[1:]
            )
        )

    def test_verified_bye_gets_no_residual_share(self):
        ros = replace(_ros(), projected_fantasy_points=30.0, raw_projected_stats={})
        rows = _materialize(
            (ros,),
            nfl_schedule=_schedule(with_bye=True),
        )

        self.assertEqual(rows[1].status, ProjectionStatus.BYE)
        self.assertIsNone(rows[1].projected_fantasy_points)
        self.assertEqual(
            fsum(
                row.projected_fantasy_points
                for row in (rows[0], rows[2])
            ),
            30.0,
        )
        self.assertLess(
            rows[0].projected_fantasy_points,
            rows[2].projected_fantasy_points,
        )

    def test_materializer_rejects_allocation_for_another_snapshot_week(self):
        with self.assertRaisesRegex(ValueError, "season, as-of week"):
            _materialize((_ros(),), allocation=replace(_allocation(), as_of_week=2))
        with self.assertRaisesRegex(ValueError, "season, as-of week"):
            _materialize((_ros(),), allocation=replace(_allocation(), season=2025))
        with self.assertRaisesRegex(ValueError, "scoring profile"):
            _materialize(
                (_ros(),),
                allocation=replace(
                    _allocation(),
                    scoring_profile_id="another-profile",
                ),
            )


if __name__ == "__main__":
    unittest.main()
