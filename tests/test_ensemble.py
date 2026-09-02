from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
import sys
import unittest

from trade_snapshot.ensemble import (
    EnsembleConfig,
    ProviderWeight,
    ensemble_from_record,
    ensemble_to_record,
    fuse_weekly_projections,
)
from trade_snapshot.projections import ProjectionStatus, WeeklyProjection


CAPTURED_AT = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)


class EnsembleConfigTests(unittest.TestCase):
    def test_config_is_immutable_and_defensively_copies_position_floors(self):
        floors = {"RB": 3.0, "WR": 4.0}
        config = make_config(position_stddev_floors=floors)

        floors["RB"] = 99.0
        self.assertEqual(config.position_stddev_floors["RB"], 3.0)
        self.assertEqual(hash(config), hash(config))
        with self.assertRaises(TypeError):
            config.position_stddev_floors["RB"] = 2.0
        with self.assertRaises(FrozenInstanceError):
            config.minimum_observed_sources = 1

    def test_config_rejects_invalid_weights_floors_and_minimum(self):
        for weight in (0, -1, math.inf, math.nan, True):
            with self.subTest(weight=weight):
                with self.assertRaises(ValueError):
                    ProviderWeight("fantasypros", weight)
        with self.assertRaises(ValueError):
            ProviderWeight("fantasypros", Fraction(1, 10**10000))

        for floor in (-1, math.inf, math.nan, True):
            with self.subTest(floor=floor):
                with self.assertRaises(ValueError):
                    make_config(position_stddev_floors={"RB": floor})

        with self.assertRaisesRegex(ValueError, "duplicate provider"):
            EnsembleConfig(
                provider_weights=(ProviderWeight("espn", 1), ProviderWeight("espn", 2)),
                minimum_observed_sources=1,
                position_stddev_floors={"RB": 1},
            )
        with self.assertRaisesRegex(ValueError, "duplicate position"):
            make_config(position_stddev_floors={"rb": 1, "RB": 2})
        for minimum in (0, 4, True):
            with self.subTest(minimum=minimum):
                with self.assertRaises(ValueError):
                    make_config(minimum_observed_sources=minimum)


class WeeklyEnsembleTests(unittest.TestCase):
    def test_weighted_mean_renormalizes_and_preserves_zero_and_unavailable(self):
        rows = (
            make_projection("fantasypros", 0),
            make_projection("espn", None, ProjectionStatus.NOT_PUBLISHED),
            make_projection("yahoo", 12),
        )

        result = fuse_weekly_projections(rows, position="rb", config=make_config())

        self.assertIs(result.status, ProjectionStatus.OBSERVED)
        self.assertEqual(result.projected_fantasy_points, 4.0)
        self.assertEqual(result.observed_source_count, 2)
        self.assertAlmostEqual(result.between_provider_stddev, math.sqrt(32))
        self.assertAlmostEqual(result.predictive_stddev, math.sqrt(41))
        self.assertEqual(
            tuple((item.provider, item.status, item.projected_fantasy_points) for item in result.provider_observations),
            (
                ("fantasypros", ProjectionStatus.OBSERVED, 0.0),
                ("espn", ProjectionStatus.NOT_PUBLISHED, None),
                ("yahoo", ProjectionStatus.OBSERVED, 12.0),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            result.projected_fantasy_points = 10
        with self.assertRaisesRegex(ValueError, "status"):
            replace(result, status="observed")

    def test_predictive_uncertainty_formula_uses_position_floor(self):
        result = fuse_weekly_projections(
            (
                make_projection("fantasypros", 10),
                make_projection("espn", 14),
                make_projection("yahoo", 12),
            ),
            position="WR",
            config=make_config(position_stddev_floors={"RB": 3, "WR": 4}),
        )

        expected_mean = 11.5
        variance = (2 * (10 - expected_mean) ** 2 + (14 - expected_mean) ** 2 + (12 - expected_mean) ** 2) / 4
        self.assertEqual(result.projected_fantasy_points, expected_mean)
        self.assertAlmostEqual(result.between_provider_stddev, math.sqrt(variance))
        self.assertAlmostEqual(result.predictive_stddev, math.sqrt(4**2 + variance))

    def test_extreme_finite_values_and_weights_do_not_overflow(self):
        config = EnsembleConfig(
            provider_weights=(
                ProviderWeight("fantasypros", 1e308),
                ProviderWeight("espn", 1e308),
                ProviderWeight("yahoo", 1e308),
            ),
            minimum_observed_sources=2,
            position_stddev_floors={"RB": 3},
        )
        result = fuse_weekly_projections(
            (
                make_projection("fantasypros", 1e200),
                make_projection("espn", -1e200),
                make_projection("yahoo", None, ProjectionStatus.NOT_PUBLISHED),
            ),
            "RB",
            config,
        )

        self.assertEqual(result.projected_fantasy_points, 0.0)
        self.assertEqual(result.between_provider_stddev, 1e200)
        self.assertEqual(result.predictive_stddev, 1e200)

        providers = ("a", "b", "c", "d")
        weights = (12406484271974.576, 1.3083879156488584e89, 1.0697798020554897e83, 2.4065675944243345e-83)
        max_config = EnsembleConfig(
            provider_weights=tuple(
                ProviderWeight(provider, weight)
                for provider, weight in zip(providers, weights)
            ),
            minimum_observed_sources=4,
            position_stddev_floors={"RB": 3},
        )
        maximum = fuse_weekly_projections(
            tuple(make_projection(provider, sys.float_info.max) for provider in providers),
            "RB",
            max_config,
        )
        self.assertEqual(maximum.projected_fantasy_points, sys.float_info.max)
        self.assertEqual(maximum.between_provider_stddev, 0.0)

    def test_observed_rows_may_share_an_explicitly_absent_game_context(self):
        rows = tuple(
            replace(
                make_projection(provider, 10),
                nfl_team_id=None,
                nfl_game_id=None,
                opponent_team_id=None,
                is_home=None,
            )
            for provider in ("fantasypros", "espn", "yahoo")
        )

        result = fuse_weekly_projections(rows, "RB", make_config())

        self.assertIsNone(result.nfl_team_id)
        self.assertIsNone(result.nfl_game_id)

    def test_bye_requires_every_configured_provider_to_report_bye(self):
        bye_rows = tuple(make_bye(provider) for provider in ("fantasypros", "espn", "yahoo"))
        result = fuse_weekly_projections(bye_rows, position="RB", config=make_config())

        self.assertIs(result.status, ProjectionStatus.BYE)
        self.assertIsNone(result.projected_fantasy_points)
        self.assertIsNone(result.between_provider_stddev)
        self.assertIsNone(result.predictive_stddev)
        self.assertEqual(result.observed_source_count, 0)

        not_all_bye = (bye_rows[0], replace(bye_rows[1], status=ProjectionStatus.NOT_PUBLISHED), bye_rows[2])
        with self.assertRaisesRegex(ValueError, "insufficient observed"):
            fuse_weekly_projections(not_all_bye, position="RB", config=make_config())

    def test_rejects_duplicate_missing_and_unconfigured_providers(self):
        complete = (
            make_projection("fantasypros", 10),
            make_projection("espn", 11),
            make_projection("yahoo", 12),
        )
        with self.assertRaisesRegex(ValueError, "duplicate provider"):
            fuse_weekly_projections((complete[0], complete[0], complete[2]), "RB", make_config())
        with self.assertRaisesRegex(ValueError, "missing required provider"):
            fuse_weekly_projections(complete[:2], "RB", make_config())
        with self.assertRaisesRegex(ValueError, "unconfigured provider"):
            fuse_weekly_projections((*complete, make_projection("other", 9)), "RB", make_config())

    def test_rejects_mixed_identity_or_game_context(self):
        baseline = make_projection("fantasypros", 10)
        variants = {
            "canonical_player_id": "p2",
            "snapshot_id": "snapshot-2",
            "scoring_profile_id": "profile-2",
            "season": 2027,
            "week": 2,
            "nfl_team_id": "DET",
            "nfl_game_id": "game-2",
            "opponent_team_id": "DET",
            "is_home": False,
        }
        for field, value in variants.items():
            with self.subTest(field=field):
                rows = (
                    baseline,
                    replace(make_projection("espn", 11), **{field: value}),
                    make_projection("yahoo", 12),
                )
                with self.assertRaisesRegex(ValueError, "identity and game context"):
                    fuse_weekly_projections(rows, "RB", make_config())

    def test_rejects_insufficient_observed_sources_and_unknown_position(self):
        rows = (
            make_projection("fantasypros", 0),
            make_projection("espn", None, ProjectionStatus.PARSE_ERROR),
            make_projection("yahoo", None, ProjectionStatus.NOT_PUBLISHED),
        )
        with self.assertRaisesRegex(ValueError, "insufficient observed"):
            fuse_weekly_projections(rows, "RB", make_config())
        with self.assertRaisesRegex(ValueError, "uncertainty floor"):
            fuse_weekly_projections(
                tuple(make_projection(provider, 10) for provider in ("fantasypros", "espn", "yahoo")),
                "TE",
                make_config(),
            )

    def test_strict_json_record_round_trip_is_lossless(self):
        result = fuse_weekly_projections(
            (
                make_projection("fantasypros", 0),
                make_projection("espn", None, ProjectionStatus.NOT_PUBLISHED),
                make_projection("yahoo", 12),
            ),
            "RB",
            make_config(),
        )

        record = ensemble_to_record(result)
        json.dumps(record, allow_nan=False)
        self.assertEqual(ensemble_from_record(record), result)

        invalid_records = (
            {**record, "extra": True},
            {**record, "projected_fantasy_points": 99},
            {**record, "projected_fantasy_points": record["projected_fantasy_points"] + 1e-13},
            {**record, "schema_version": True},
            {**record, "provider_observations": [*record["provider_observations"], record["provider_observations"][0]]},
        )
        for invalid in invalid_records:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    ensemble_from_record(invalid)

        same_team_opponent = {**record, "opponent_team_id": record["nfl_team_id"]}
        with self.assertRaisesRegex(ValueError, "opponent_team_id"):
            ensemble_from_record(same_team_opponent)

        bye = fuse_weekly_projections(
            tuple(make_bye(provider) for provider in ("fantasypros", "espn", "yahoo")),
            "RB",
            make_config(),
        )
        self.assertEqual(ensemble_from_record(ensemble_to_record(bye)), bye)


def make_config(**changes) -> EnsembleConfig:
    values = {
        "provider_weights": (
            ProviderWeight("fantasypros", 2),
            ProviderWeight("espn", 1),
            ProviderWeight("yahoo", 1),
        ),
        "minimum_observed_sources": 2,
        "position_stddev_floors": {"RB": 3.0},
    }
    values.update(changes)
    return EnsembleConfig(**values)


def make_projection(
    provider: str,
    points: float | None,
    status: ProjectionStatus = ProjectionStatus.OBSERVED,
) -> WeeklyProjection:
    return WeeklyProjection(
        canonical_player_id="p1",
        snapshot_id="snapshot-1",
        scoring_profile_id="profile-1",
        provider=provider,
        provider_player_id=f"{provider}-p1",
        season=2026,
        week=1,
        status=status,
        captured_at=CAPTURED_AT,
        projected_fantasy_points=points,
        nfl_team_id="GB",
        nfl_game_id="game-1",
        opponent_team_id="CHI",
        is_home=True,
    )


def make_bye(provider: str) -> WeeklyProjection:
    return WeeklyProjection(
        canonical_player_id="p1",
        snapshot_id="snapshot-1",
        scoring_profile_id="profile-1",
        provider=provider,
        provider_player_id=f"{provider}-p1",
        season=2026,
        week=1,
        status=ProjectionStatus.BYE,
        captured_at=CAPTURED_AT,
        nfl_team_id="GB",
    )


if __name__ == "__main__":
    unittest.main()
