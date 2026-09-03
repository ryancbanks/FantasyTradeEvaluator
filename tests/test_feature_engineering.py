from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt
import unittest

from tests.ecr_fixtures import ecr_source_provenance
from trade_snapshot.ecr import EcrExpertPanel, EcrPeriod, EcrPlayerRanking, EcrSnapshot
from trade_snapshot._ensemble_math import weighted_metrics
from trade_snapshot.ensemble import EnsembleProjection, ProviderObservation
from trade_snapshot.feature_engineering import (
    ProjectionAvailabilityRequirements,
    build_strength_features,
    feature_names,
    projection_availability_requirements,
)
from trade_snapshot.projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)
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
    rows = tuple(rankings)
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
        rankings=rows,
        expert_panels=(EcrExpertPanel(
            "RB",
            ("9", "22"),
            2,
            ecr_source_provenance(
                captured_at=NOW,
                source_updated_at=NOW - timedelta(hours=1),
                horizon=("weekly" if period is EcrPeriod.WEEKLY else "ros"),
                source_player_count=len(rows),
            ),
        ),),
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


def _horizon_evidence(projections, applicable_weeks, totals):
    rows = []
    for projection_row in projections:
        for observation in projection_row.provider_observations:
            rows.append(
                WeeklyProjection(
                    projection_row.canonical_player_id,
                    projection_row.snapshot_id,
                    projection_row.scoring_profile_id,
                    observation.provider,
                    observation.provider_player_id,
                    projection_row.season,
                    projection_row.week,
                    observation.status,
                    NOW,
                    observation.projected_fantasy_points,
                    origin=WeeklyProjectionOrigin.PROVIDER_PUBLISHED,
                )
            )
    template = projections[0]
    for player_id, provider_totals in totals.items():
        for provider, total in provider_totals.items():
            rows.append(
                RemainingSeasonProjection(
                    player_id,
                    template.snapshot_id,
                    template.scoring_profile_id,
                    provider,
                    f"{provider}-{player_id}",
                    template.season,
                    applicable_weeks,
                    ProjectionStatus.OBSERVED,
                    RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                    NOW,
                    total,
                )
            )
    return tuple(rows)


def grid_horizon_evidence(projections):
    return _horizon_evidence(
        projections,
        (1, 2),
        {
            "p1": {"fantasypros": 30, "espn": 34, "yahoo": 38},
            "p2": {"fantasypros": 22, "espn": 26, "yahoo": 30},
        },
    )


def full_horizon_evidence(projections):
    return _horizon_evidence(
        projections,
        (1, 2, 3, 4),
        {
        "p1": {"fantasypros": 50, "espn": 60, "yahoo": 70},
        "p2": {"fantasypros": 40, "espn": 50, "yahoo": 60},
        },
    )


def grid_feature_kwargs(projections):
    return {
        "projection_evidence": grid_horizon_evidence(projections),
        "remaining_week_scopes": {"p1": (1, 2), "p2": (1, 2)},
    }


class StrengthFeatureEngineeringTests(unittest.TestCase):
    def test_preserves_provider_and_ecr_signals_with_explicit_missingness(self):
        snapshots, projections, eligibility = inputs()
        result = build_strength_features(
            snapshots, projections, eligibility, **grid_feature_kwargs(projections)
        )
        by_player = {row.player_id: row for row in result.player_features}
        p1, p2 = by_player["p1"].values, by_player["p2"].values

        self.assertEqual(result.weeks, (1, 2))
        self.assertEqual(result.feature_names, feature_names())
        self.assertEqual(len(result.feature_names), 39)
        self.assertEqual(p1["projection_espn_current_points"], 12)
        self.assertEqual(p1["projection_espn_full_ros_points"], 34)
        self.assertAlmostEqual(p1["projection_ensemble_full_ros_points"], 34)
        self.assertAlmostEqual(p1["projection_ensemble_full_ros_mean"], 17)
        self.assertEqual(p1["projection_espn_full_ros_available"], 1)
        self.assertEqual(p1["projection_ensemble_full_ros_available"], 1)
        self.assertAlmostEqual(
            p1["projection_ensemble_regular_season_uncertainty"], sqrt(16 / 3)
        )
        self.assertEqual(p1["ecr_weekly_available"], 1)
        self.assertEqual(p2["ecr_weekly_available"], 0)
        self.assertEqual(p2["ecr_weekly_inverse_rank"], 0)
        self.assertEqual(p2["projection_yahoo_current_available"], 1)

    def test_input_order_does_not_change_feature_identity(self):
        snapshots, projections, eligibility = inputs()
        first = build_strength_features(
            snapshots, projections, eligibility, **grid_feature_kwargs(projections)
        )
        second = build_strength_features(
            reversed(snapshots),
            reversed(projections),
            reversed(eligibility),
            projection_evidence=reversed(grid_horizon_evidence(projections)),
            remaining_week_scopes={"p2": (1, 2), "p1": (1, 2)},
        )
        self.assertEqual(first, second)
        self.assertTrue(first.feature_set_id.startswith("features_"))

    def test_uses_full_nfl_horizon_for_value_features_when_evidence_is_supplied(self):
        snapshots, projections, eligibility = inputs()
        result = build_strength_features(
            snapshots,
            projections,
            eligibility,
            projection_evidence=full_horizon_evidence(projections),
            remaining_week_scopes={"p1": (1, 2, 3, 4), "p2": (1, 2, 3, 4)},
        )
        p1 = next(
            row.values for row in result.player_features if row.player_id == "p1"
        )

        self.assertEqual(p1["projection_espn_full_ros_points"], 60)
        self.assertEqual(p1["projection_ensemble_full_ros_points"], 60)
        self.assertEqual(p1["projection_ensemble_full_ros_mean"], 15)
        self.assertEqual(p1["projection_espn_current_points"], 12)

    def test_marks_optional_full_horizon_sources_unavailable_and_honors_quorum(self):
        snapshots, projections, eligibility = inputs()
        for missing_providers in ({"fantasypros"}, {"espn", "yahoo"}):
            with self.subTest(missing_providers=missing_providers):
                evidence = tuple(
                    row
                    for row in full_horizon_evidence(projections)
                    if not (
                        isinstance(row, RemainingSeasonProjection)
                        and row.canonical_player_id == "p1"
                        and row.provider in missing_providers
                    )
                )

                result = build_strength_features(
                    snapshots,
                    projections,
                    eligibility,
                    projection_evidence=evidence,
                    remaining_week_scopes={
                        "p1": (1, 2, 3, 4),
                        "p2": (1, 2, 3, 4),
                    },
                )
                p1 = next(
                    row.values
                    for row in result.player_features
                    if row.player_id == "p1"
                )
                for provider in missing_providers:
                    self.assertEqual(
                        p1[f"projection_{provider}_full_ros_available"], 0
                    )
                    self.assertEqual(p1[f"projection_{provider}_full_ros_points"], 0)
                ensemble_available = float(len(missing_providers) == 1)
                self.assertEqual(
                    p1["projection_ensemble_full_ros_available"],
                    ensemble_available,
                )

    def test_requires_explicit_full_horizon_evidence_and_scopes(self):
        snapshots, projections, eligibility = inputs()

        with self.assertRaises(TypeError):
            build_strength_features(snapshots, projections, eligibility)

    def test_formula_features_identify_only_mandatory_projection_sources(self):
        configured = ("fantasypros", "espn", "yahoo")

        self.assertEqual(
            projection_availability_requirements(
                (
                    "projection_fantasypros_full_ros_points",
                    "projection_espn_current_points",
                    "projection_ensemble_full_ros_points",
                    "projection_yahoo_full_ros_available",
                ),
                configured,
            ),
            ProjectionAvailabilityRequirements(
                current_providers=frozenset({"espn"}),
                full_ros_providers=frozenset({"fantasypros"}),
                ensemble_current=False,
                ensemble_full_ros=True,
            ),
        )
        with self.assertRaisesRegex(ValueError, "no configured provider"):
            projection_availability_requirements(
                ("projection_sleeper_current_points",),
                configured,
            )

    def test_rejects_missing_provider_rows_incomplete_grid_and_identity_drift(self):
        snapshots, projections, eligibility = inputs()
        partial_observations = projections[0].provider_observations[:2]
        mean, disagreement, predictive = weighted_metrics(partial_observations, 0)
        missing_provider = replace(
            projections[0],
            provider_observations=partial_observations,
            projected_fantasy_points=mean,
            between_provider_stddev=disagreement,
            predictive_stddev=predictive,
        )
        with self.assertRaisesRegex(ValueError, "explicit provider evidence"):
            build_strength_features(
                snapshots,
                (missing_provider, *projections[1:]),
                eligibility,
                **grid_feature_kwargs(projections),
            )
        extra_observations = (
            *projections[0].provider_observations,
            ProviderObservation(
                "sleeper",
                "sleeper-p1",
                ProjectionStatus.OBSERVED,
                13,
                1,
            ),
        )
        mean, disagreement, predictive = weighted_metrics(extra_observations, 0)
        extra_provider = replace(
            projections[0],
            provider_observations=extra_observations,
            projected_fantasy_points=mean,
            between_provider_stddev=disagreement,
            predictive_stddev=predictive,
        )
        with self.assertRaisesRegex(ValueError, "unconfigured provider"):
            build_strength_features(
                snapshots,
                (extra_provider, *projections[1:]),
                eligibility,
                **grid_feature_kwargs(projections),
            )
        with self.assertRaisesRegex(ValueError, "complete grid"):
            build_strength_features(
                snapshots,
                projections[:-1],
                eligibility,
                **grid_feature_kwargs(projections),
            )
        bad_snapshots = (snapshots[0], replace(snapshots[1], snapshot_id="changed"))
        with self.assertRaisesRegex(ValueError, "one identity"):
            build_strength_features(
                bad_snapshots,
                projections,
                eligibility,
                **grid_feature_kwargs(projections),
            )


if __name__ == "__main__":
    unittest.main()
