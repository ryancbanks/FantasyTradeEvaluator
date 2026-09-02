import unittest

from tests.test_weekly_assembly import (
    host_snapshot,
    nfl_schedule,
    projection_artifact,
)
from trade_snapshot.capture_schema import CaptureProvider, RankingHorizon
from trade_snapshot.engine_bundle import EngineBundle
from trade_snapshot.independent_waiver_pool import IndependentWaiverPool
from trade_snapshot.independent_weekly_assembly import (
    assemble_independent_weekly_engine,
)


def projection_artifacts(*, broad):
    providers = (
        (
            CaptureProvider.ESPN,
            (RankingHorizon.WEEKLY, RankingHorizon.ROS),
        ),
        (
            CaptureProvider.YAHOO,
            (RankingHorizon.WEEKLY, RankingHorizon.ROS),
        ),
    )
    if broad:
        providers += (
            (CaptureProvider.CBS, (RankingHorizon.ROS,)),
            (
                CaptureProvider.FFTODAY,
                (RankingHorizon.WEEKLY, RankingHorizon.ROS),
            ),
            (
                CaptureProvider.FANTASYSHARKS,
                (RankingHorizon.WEEKLY, RankingHorizon.ROS),
            ),
        )
    return tuple(
        projection_artifact(provider, horizon, week)
        for provider, horizons in providers
        for horizon in horizons
        for week in ((1, 2) if horizon is RankingHorizon.WEEKLY else (1,))
    )


class IndependentWeeklyAssemblyTests(unittest.TestCase):
    def test_builds_round_trippable_broad_engine_without_fantasypros(self):
        assembled = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=projection_artifacts(broad=True),
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=True,
        )
        bundle = assembled.bundle

        self.assertEqual(bundle.methodology_mode, "independent")
        self.assertEqual(bundle.ecr_snapshots, ())
        self.assertIsInstance(bundle.waiver_pool, IndependentWaiverPool)
        self.assertEqual(
            bundle.independent_power_disclosure.provider_names,
            ("cbs", "espn", "fantasysharks", "fftoday", "yahoo"),
        )
        observation_providers = {
            observation.provider
            for row in bundle.projections
            for observation in row.provider_observations
        }
        self.assertEqual(
            observation_providers,
            {"espn", "yahoo", "cbs", "fftoday", "fantasysharks"},
        )
        self.assertNotIn(
            "fantasypros",
            {row.provider for row in bundle.projection_evidence},
        )
        self.assertEqual(EngineBundle.from_record(bundle.to_record()), bundle)

    def test_core_fallback_uses_espn_and_yahoo(self):
        bundle = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=projection_artifacts(broad=False),
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=False,
        ).bundle

        self.assertEqual(
            bundle.independent_power_disclosure.provider_names, ("espn", "yahoo")
        )
        self.assertEqual(
            {
                observation.provider
                for row in bundle.projections
                for observation in row.provider_observations
            },
            {"espn", "yahoo"},
        )

    def test_accepts_ros_only_fftoday_evidence_for_idp_safe_fallback(self):
        artifacts = tuple(
            row
            for row in projection_artifacts(broad=True)
            if not (
                row.provider is CaptureProvider.FFTODAY
                and row.horizon is RankingHorizon.WEEKLY
            )
        )

        bundle = assemble_independent_weekly_engine(
            host_snapshot=host_snapshot(),
            projection_artifacts=artifacts,
            nfl_schedule=nfl_schedule(),
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=True,
        ).bundle

        self.assertIn(
            "fftoday",
            bundle.independent_power_disclosure.provider_names,
        )

    def test_core_ensemble_requires_yahoo_projection_evidence(self):
        artifacts = tuple(
            row
            for row in projection_artifacts(broad=False)
            if row.provider is not CaptureProvider.YAHOO
        )

        with self.assertRaisesRegex(ValueError, "ESPN and Yahoo"):
            assemble_independent_weekly_engine(
                host_snapshot=host_snapshot(),
                projection_artifacts=artifacts,
                nfl_schedule=nfl_schedule(),
                scoring="PPR",
                expected_team_count=2,
                broad_consensus=False,
            )


if __name__ == "__main__":
    unittest.main()
