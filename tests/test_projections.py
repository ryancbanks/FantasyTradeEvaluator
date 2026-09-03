from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import math
import unittest

from trade_snapshot.projections import (
    ProjectionStatus,
    ProviderStatusObservation,
    ProviderStatusScope,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
    derive_remaining_season,
)
from trade_snapshot.projection_io import projection_to_record


CAPTURED_AT = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
PUBLISHED_AT = CAPTURED_AT - timedelta(hours=1)


class WeeklyProjectionTests(unittest.TestCase):
    def test_observed_zero_and_raw_stats_are_preserved_immutably(self):
        supplied_stats = {"pass_yards": 0, "interceptions": 1.0}
        projection = make_weekly(
            projected_fantasy_points=0,
            raw_projected_stats=supplied_stats,
        )

        supplied_stats["pass_yards"] = 400
        self.assertEqual(projection.projected_fantasy_points, 0.0)
        self.assertEqual(
            dict(projection.raw_projected_stats),
            {"pass_yards": 0.0, "interceptions": 1.0},
        )
        with self.assertRaises(TypeError):
            projection.raw_projected_stats["pass_yards"] = 300
        original_hash = hash(projection)
        with self.assertRaises(TypeError):
            dict.__setitem__(projection.raw_projected_stats, "evil", 2.0)
        with self.assertRaises(FrozenInstanceError):
            projection.raw_projected_stats._items = (("evil", 2.0),)
        self.assertEqual(hash(projection), original_hash)
        self.assertEqual(
            projection_to_record(projection)["raw_projected_stats"],
            {"interceptions": 1.0, "pass_yards": 0.0},
        )
        self.assertEqual(projection_to_record(projection)["status"], "observed")
        self.assertEqual(
            projection_to_record(projection)["captured_at"],
            "2026-09-01T15:00:00.000000Z",
        )
        with self.assertRaises(FrozenInstanceError):
            projection.week = 2

    def test_observed_requires_finite_points(self):
        for value in (None, True, math.inf, -math.inf, math.nan, 10**10000):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite projected_fantasy_points"):
                    make_weekly(projected_fantasy_points=value)

    def test_every_non_observed_state_rejects_numeric_points_and_raw_stats(self):
        for status in ProjectionStatus:
            if status is ProjectionStatus.OBSERVED:
                continue
            status_context = {"status": status}
            if status is ProjectionStatus.BYE:
                status_context.update(
                    nfl_game_id=None,
                    opponent_team_id=None,
                    is_home=None,
                )
            if status is ProjectionStatus.UNMATCHED_PLAYER:
                status_context["canonical_player_id"] = None
            with self.subTest(status=status, field="points"):
                with self.assertRaisesRegex(ValueError, "must be absent"):
                    make_weekly(**status_context, projected_fantasy_points=0)
            with self.subTest(status=status, field="stats"):
                with self.assertRaisesRegex(ValueError, "raw_projected_stats must be empty"):
                    make_weekly(
                        **status_context,
                        projected_fantasy_points=None,
                        raw_projected_stats={"targets": 0},
                    )

    def test_raw_stats_require_named_finite_numeric_values(self):
        invalid_stats = (
            {"": 1},
            {"yards": True},
            {"yards": math.nan},
            {"yards": math.inf},
        )
        for stats in invalid_stats:
            with self.subTest(stats=stats):
                with self.assertRaises(ValueError):
                    make_weekly(raw_projected_stats=stats)

    def test_unmatched_rows_are_the_only_rows_without_a_canonical_player(self):
        unmatched = make_weekly(
            canonical_player_id=None,
            status=ProjectionStatus.UNMATCHED_PLAYER,
            projected_fantasy_points=None,
        )
        self.assertIsNone(unmatched.canonical_player_id)

        with self.assertRaisesRegex(ValueError, "canonical_player_id"):
            make_weekly(canonical_player_id=None)
        with self.assertRaisesRegex(ValueError, "must be absent"):
            make_weekly(status=ProjectionStatus.UNMATCHED_PLAYER, projected_fantasy_points=None)

    def test_bye_has_team_context_but_no_game_context(self):
        bye = make_weekly(
            status=ProjectionStatus.BYE,
            projected_fantasy_points=None,
            nfl_game_id=None,
            opponent_team_id=None,
            is_home=None,
        )
        self.assertEqual(bye.nfl_team_id, "GB")

        with self.assertRaisesRegex(ValueError, "bye projection cannot have NFL game context"):
            make_weekly(status=ProjectionStatus.BYE, projected_fantasy_points=None)
        with self.assertRaisesRegex(ValueError, "bye projection requires nfl_team_id"):
            make_weekly(
                status=ProjectionStatus.BYE,
                projected_fantasy_points=None,
                nfl_team_id=None,
                nfl_game_id=None,
                opponent_team_id=None,
                is_home=None,
            )

    def test_game_context_and_timestamps_must_be_complete(self):
        with self.assertRaisesRegex(ValueError, "game context"):
            make_weekly(opponent_team_id=None)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            make_weekly(captured_at=CAPTURED_AT.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "cannot be after captured_at"):
            make_weekly(source_published_at=CAPTURED_AT + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "requires nfl_team_id"):
            make_weekly(nfl_team_id=None)

    def test_provider_status_is_bounded_observation_not_projection_availability(self):
        observation = ProviderStatusObservation(
            "Questionable",
            CAPTURED_AT,
            ProviderStatusScope.WEEKLY,
            1,
        )
        projection = make_weekly(provider_status_observations=(observation,))

        self.assertIs(projection.status, ProjectionStatus.OBSERVED)
        self.assertEqual(projection.provider_status_observations, (observation,))
        for designation in (" Q ", "https://example.test/status", "x" * 81):
            with self.subTest(designation=designation), self.assertRaises(ValueError):
                ProviderStatusObservation(
                    designation,
                    CAPTURED_AT,
                    ProviderStatusScope.WEEKLY,
                    1,
                )
        with self.assertRaisesRegex(ValueError, "newer"):
            make_weekly(
                provider_status_observations=(
                    ProviderStatusObservation(
                        "Out",
                        CAPTURED_AT + timedelta(seconds=1),
                        ProviderStatusScope.WEEKLY,
                        1,
                    ),
                )
            )


class RemainingSeasonProjectionTests(unittest.TestCase):
    def test_ros_is_a_separate_type_with_the_same_missing_value_contract(self):
        ros = RemainingSeasonProjection(
            canonical_player_id="player-1",
            snapshot_id="snapshot-1",
            scoring_profile_id="ppr-default-v1",
            provider="fantasypros",
            provider_player_id="fp-1",
            season=2026,
            applicable_weeks=(1, 2, 4),
            status=ProjectionStatus.OBSERVED,
            origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
            captured_at=CAPTURED_AT,
            projected_fantasy_points=42,
            raw_projected_stats={"targets": 20},
            source_published_at=PUBLISHED_AT,
        )

        self.assertNotIsInstance(ros, WeeklyProjection)
        self.assertIs(ros.origin, RemainingSeasonOrigin.PROVIDER_PUBLISHED)
        self.assertEqual(ros.applicable_weeks, (1, 2, 4))
        with self.assertRaisesRegex(ValueError, "must be absent"):
            replace(
                ros,
                status=ProjectionStatus.NOT_PUBLISHED,
                projected_fantasy_points=0,
                raw_projected_stats={},
            )
        with self.assertRaisesRegex(ValueError, "not valid for a remaining-season"):
            replace(
                ros,
                status=ProjectionStatus.BYE,
                projected_fantasy_points=None,
                raw_projected_stats={},
            )

        not_applicable = replace(
            ros,
            status=ProjectionStatus.NOT_APPLICABLE,
            applicable_weeks=(),
            projected_fantasy_points=None,
            raw_projected_stats={},
        )
        self.assertEqual(not_applicable.applicable_weeks, ())

    def test_derives_only_from_one_observed_row_per_applicable_week(self):
        week1 = make_weekly(
            week=1,
            projected_fantasy_points=10.25,
            raw_projected_stats={"targets": 6, "receiving_yards": 55},
            provider_status_observations=(
                ProviderStatusObservation(
                    "Q",
                    CAPTURED_AT,
                    ProviderStatusScope.WEEKLY,
                    1,
                ),
            ),
        )
        week2 = make_weekly(
            week=2,
            projected_fantasy_points=12.75,
            raw_projected_stats={"targets": 8, "receiving_yards": 70},
            captured_at=CAPTURED_AT + timedelta(hours=1),
            source_published_at=None,
        )
        excluded_bye = make_weekly(
            week=3,
            status=ProjectionStatus.BYE,
            projected_fantasy_points=None,
            nfl_game_id=None,
            opponent_team_id=None,
            is_home=None,
        )

        ros = derive_remaining_season((week1, week2, excluded_bye), (1, 2))

        self.assertEqual(ros.applicable_weeks, (1, 2))
        self.assertIs(ros.origin, RemainingSeasonOrigin.DERIVED_WEEKLY)
        self.assertEqual(ros.projected_fantasy_points, 23.0)
        self.assertEqual(
            dict(ros.raw_projected_stats),
            {"targets": 14.0, "receiving_yards": 125.0},
        )
        self.assertEqual(ros.captured_at, week2.captured_at)
        self.assertIsNone(ros.source_published_at)
        self.assertEqual(
            ros.provider_status_observations,
            week1.provider_status_observations,
        )

    def test_derivation_rejects_missing_non_observed_and_duplicate_weeks(self):
        week1 = make_weekly(week=1)
        week2 = make_weekly(week=2)

        with self.subTest("missing"):
            with self.assertRaisesRegex(ValueError, "missing projection for applicable week 2"):
                derive_remaining_season((week1,), (1, 2))

        with self.subTest("non-observed"):
            missing = replace(
                week2,
                status=ProjectionStatus.NOT_PUBLISHED,
                projected_fantasy_points=None,
            )
            with self.assertRaisesRegex(ValueError, "must be observed"):
                derive_remaining_season((week1, missing), (1, 2))

        with self.subTest("duplicate row"):
            with self.assertRaisesRegex(ValueError, "duplicate weekly projection for week 1"):
                derive_remaining_season((week1, week1, week2), (1, 2))

        with self.subTest("duplicate applicable week"):
            with self.assertRaisesRegex(ValueError, "applicable_weeks cannot contain duplicates"):
                derive_remaining_season((week1, week2), (1, 1, 2))

    def test_derivation_rejects_mixed_identity_and_incomplete_stat_series(self):
        week1 = make_weekly(week=1, raw_projected_stats={"targets": 6})

        with self.subTest("identity"):
            other_player = make_weekly(week=2, canonical_player_id="player-2")
            with self.assertRaisesRegex(ValueError, "snapshot, and scoring profile"):
                derive_remaining_season((week1, other_player), (1, 2))

        for field, value in (
            ("snapshot_id", "snapshot-2"),
            ("scoring_profile_id", "standard-v1"),
        ):
            with self.subTest(field=field):
                mixed = make_weekly(week=2, **{field: value})
                with self.assertRaisesRegex(ValueError, "snapshot, and scoring profile"):
                    derive_remaining_season((week1, mixed), (1, 2))

        with self.subTest("stat fields"):
            incomplete = make_weekly(week=2, raw_projected_stats={})
            with self.assertRaisesRegex(ValueError, "same raw projected stat fields"):
                derive_remaining_season((week1, incomplete), (1, 2))


def make_weekly(**changes) -> WeeklyProjection:
    values = {
        "canonical_player_id": "player-1",
        "snapshot_id": "snapshot-1",
        "scoring_profile_id": "ppr-default-v1",
        "provider": "fantasypros",
        "provider_player_id": "fp-1",
        "season": 2026,
        "week": 1,
        "status": ProjectionStatus.OBSERVED,
        "captured_at": CAPTURED_AT,
        "projected_fantasy_points": 14.5,
        "raw_projected_stats": {},
        "nfl_team_id": "GB",
        "nfl_game_id": "2026-W01-GB-CHI",
        "opponent_team_id": "CHI",
        "is_home": True,
        "source_published_at": PUBLISHED_AT,
    }
    values.update(changes)
    return WeeklyProjection(**values)


if __name__ == "__main__":
    unittest.main()
