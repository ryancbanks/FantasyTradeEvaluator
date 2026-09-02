from datetime import datetime, timezone
import unittest

from trade_snapshot.projection_io import projection_from_record, projection_to_record
from trade_snapshot.projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
)


CAPTURED = datetime(2026, 9, 1, 15, 0, 0, 123456, tzinfo=timezone.utc)


class ProjectionRecordTests(unittest.TestCase):
    def test_weekly_round_trip_is_lossless(self):
        projection = WeeklyProjection(
            canonical_player_id="p1",
            snapshot_id="snapshot-1",
            scoring_profile_id="profile-1",
            provider="espn",
            provider_player_id="e1",
            season=2026,
            week=1,
            status=ProjectionStatus.OBSERVED,
            captured_at=CAPTURED,
            projected_fantasy_points=12.5,
            raw_projected_stats={"pass_yards": 250},
            nfl_team_id="GB",
            nfl_game_id="game-1",
            opponent_team_id="CHI",
            is_home=True,
        )

        record = projection_to_record(projection)

        self.assertEqual(record["captured_at"], "2026-09-01T15:00:00.123456Z")
        self.assertEqual(projection_from_record(record), projection)

    def test_remaining_season_round_trip_preserves_origin(self):
        projection = RemainingSeasonProjection(
            canonical_player_id="p1",
            snapshot_id="snapshot-1",
            scoring_profile_id="profile-1",
            provider="fantasypros",
            provider_player_id="f1",
            season=2026,
            applicable_weeks=(1, 2),
            status=ProjectionStatus.OBSERVED,
            origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
            captured_at=CAPTURED,
            projected_fantasy_points=25,
        )

        self.assertEqual(projection_from_record(projection_to_record(projection)), projection)

    def test_reader_rejects_unknown_fields_kind_and_bad_timestamp(self):
        base = projection_to_record(
            WeeklyProjection(
                canonical_player_id="p1",
                snapshot_id="snapshot-1",
                scoring_profile_id="profile-1",
                provider="yahoo",
                provider_player_id="y1",
                season=2026,
                week=1,
                status=ProjectionStatus.NOT_PUBLISHED,
                captured_at=CAPTURED,
            )
        )
        invalid_records = (
            {**base, "unknown": 1},
            {**base, "kind": "daily"},
            {**base, "captured_at": "not-a-time"},
        )
        for record in invalid_records:
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    projection_from_record(record)


if __name__ == "__main__":
    unittest.main()
