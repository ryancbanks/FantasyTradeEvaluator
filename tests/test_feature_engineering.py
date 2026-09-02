from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt
import unittest
from unittest.mock import patch

import trade_snapshot.feature_engineering as feature_module
from trade_snapshot._ensemble_math import weighted_metrics
from trade_snapshot.ecr import EcrPeriod, EcrPlayerRanking, EcrSnapshot
from trade_snapshot.ensemble import EnsembleProjection, ProviderObservation
from trade_snapshot.feature_engineering import build_strength_features, feature_names
from trade_snapshot.projections import ProjectionStatus
from trade_snapshot.scenario_config import PlayerEligibility


NOW = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
PROVIDERS = ("fantasypros", "espn", "yahoo")


def ecr(
    period,
    rankings,
    *,
    snapshot_id="snapshot-1",
    scoring_profile_id="profile-1",
):
    return EcrSnapshot(
        snapshot_id=snapshot_id,
        scoring_profile_id=scoring_profile_id,
        season=2026,
        as_of_week=1,
        period=period,
        captured_at=NOW,
        source_updated_at=NOW - timedelta(hours=1),
        expert_ids=("9", "22"),
        total_experts=2,
        rankings=tuple(rankings),
    )


def rank(player_id, provider_id, overall, position):
    return EcrPlayerRanking(
        player_id,
        provider_id,
        "RB",
        overall,
        position,
        overall,
        overall + 2,
        overall + 1,
        1,
    )


def projection(player_id, week, base, *, scoring_profile_id="profile-1"):
    observations = tuple(
        ProviderObservation(
            provider,
            f"{provider}-{player_id}",
            ProjectionStatus.OBSERVED,
            base + offset,
            1,
        )
        for provider, offset in zip(PROVIDERS, (-2, 0, 2))
    )
    mean, disagreement, predictive = weighted_metrics(observations, 0)
    return EnsembleProjection(
        canonical_player_id=player_id,
        snapshot_id="snapshot-1",
        scoring_profile_id=scoring_profile_id,
        season=2026,
        week=week,
        position="RB",
        status=ProjectionStatus.OBSERVED,
        provider_observations=observations,
        minimum_observed_sources=2,
        position_stddev_floor=0,
        projected_fantasy_points=mean,
        between_provider_stddev=disagreement,
        predictive_stddev=predictive,
        nfl_team_id=f"NFL-{player_id}",
        nfl_game_id=f"G{week}-{player_id}",
        opponent_team_id=f"OPP-{player_id}",
        is_home=True,
    )


def inputs(scoring_profile_id="profile-1"):
    weekly = ecr(
        EcrPeriod.WEEKLY,
        (rank("p1", "101", 1, 1),),
        scoring_profile_id=scoring_profile_id,
    )
    ros = ecr(
        EcrPeriod.REST_OF_SEASON,
        (rank("p1", "101", 2, 1), rank("free-agent", "999", 1, 1)),
        scoring_profile_id=scoring_profile_id,
    )
    projections = (
        projection("p1", 1, 12, scoring_profile_id=scoring_profile_id),
        projection("p1", 2, 22, scoring_profile_id=scoring_profile_id),
        projection("p2", 1, 8, scoring_profile_id=scoring_profile_id),
        projection("p2", 2, 18, scoring_profile_id=scoring_profile_id),
    )
    eligibility = (
        PlayerEligibility("p1", ("RB", "FLEX")),
        PlayerEligibility("p2", ("RB", "FLEX")),
    )
    return (weekly, ros), projections, eligibility


class StrengthFeatureEngineeringTests(unittest.TestCase):
    def test_snapshot_rank_totals_are_computed_once_per_feature_set(self):
        snapshots, projections, eligibility = inputs()

        with patch(
            "trade_snapshot.feature_engineering._ecr_total",
            wraps=feature_module._ecr_total,
        ) as total:
            result = build_strength_features(snapshots, projections, eligibility)

        self.assertEqual(total.call_count, 2)
        self.assertEqual(len(result.player_features), len(eligibility))

    def test_preserves_provider_and_ecr_signals_with_explicit_missingness(self):
        snapshots, projections, eligibility = inputs()
        result = build_strength_features(snapshots, projections, eligibility)
        by_player = {row.player_id: row for row in result.player_features}
        p1, p2 = by_player["p1"].values, by_player["p2"].values

        self.assertEqual(result.weeks, (1, 2))
        self.assertEqual(result.feature_names, feature_names())
        self.assertEqual(result.provider_names, ("fantasypros", "espn", "yahoo"))
        self.assertEqual(len(result.feature_names), 35)
        self.assertEqual(p1["projection_espn_current_points"], 12)
        self.assertEqual(p1["projection_espn_remaining_points"], 34)
        self.assertEqual(p1["projection_ensemble_remaining_points"], 34)
        self.assertEqual(p1["projection_ensemble_remaining_mean"], 17)
        self.assertAlmostEqual(
            p1["projection_ensemble_remaining_uncertainty"], sqrt(16 / 3)
        )
        self.assertEqual(p1["ecr_weekly_available"], 1)
        self.assertEqual(p2["ecr_weekly_available"], 0)
        self.assertEqual(p2["ecr_weekly_inverse_rank"], 0)
        self.assertEqual(p2["projection_yahoo_current_available"], 1)

    def test_unpublished_provider_is_excluded_from_remaining_projection_totals(self):
        snapshots, projections, eligibility = inputs()
        observations = (
            *projections[1].provider_observations[:2],
            replace(
                projections[1].provider_observations[2],
                status=ProjectionStatus.NOT_PUBLISHED,
                projected_fantasy_points=None,
            ),
        )
        mean, disagreement, predictive = weighted_metrics(observations, 0)
        unpublished = replace(
            projections[1],
            provider_observations=observations,
            projected_fantasy_points=mean,
            between_provider_stddev=disagreement,
            predictive_stddev=predictive,
        )

        result = build_strength_features(
            snapshots,
            (projections[0], unpublished, *projections[2:]),
            eligibility,
            provider_names=PROVIDERS,
        )
        p1 = next(
            row.values for row in result.player_features if row.player_id == "p1"
        )

        self.assertEqual(p1["projection_yahoo_observed_week_fraction"], 0.5)
        self.assertEqual(p1["projection_yahoo_remaining_points"], 14)
        self.assertEqual(p1["projection_ensemble_remaining_points"], 33)

    def test_input_order_does_not_change_feature_identity(self):
        snapshots, projections, eligibility = inputs()
        first = build_strength_features(snapshots, projections, eligibility)
        second = build_strength_features(
            reversed(snapshots), reversed(projections), reversed(eligibility)
        )
        self.assertEqual(first, second)
        self.assertTrue(first.feature_set_id.startswith("features_"))

    def test_rejects_missing_provider_rows_incomplete_grid_and_identity_drift(self):
        snapshots, projections, eligibility = inputs()
        with self.assertRaisesRegex(ValueError, "explicit provider evidence"):
            build_strength_features(
                snapshots,
                projections,
                eligibility,
                provider_names=("espn", "cbs"),
            )
        with self.assertRaisesRegex(ValueError, "complete grid"):
            build_strength_features(snapshots, projections[:-1], eligibility)
        bad_snapshots = (snapshots[0], replace(snapshots[1], snapshot_id="changed"))
        with self.assertRaisesRegex(ValueError, "one identity"):
            build_strength_features(bad_snapshots, projections, eligibility)


if __name__ == "__main__":
    unittest.main()
