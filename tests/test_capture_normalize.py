from dataclasses import replace
import unittest

from trade_snapshot.capture_normalize import (
    ecr_provider_records,
    ecr_snapshot_from_artifact,
    projection_evidence_from_artifact,
    projection_provider_records,
)
from trade_snapshot.capture_schema import (
    CaptureKind,
    CaptureProvider,
    ECRCaptureMethod,
    ECRRankingRow,
    FantasyProsECRArtifact,
    GenericTableArtifact,
    RankingHorizon,
    VisibleTable,
    VisibleTableCell,
)
from trade_snapshot.ecr import EcrPeriod
from trade_snapshot.identity_match import ProviderPlayerRecord, reconcile_player_identities
from trade_snapshot.projections import ProjectionStatus, RemainingSeasonProjection, WeeklyProjection


def artifact(*, horizon=RankingHorizon.WEEKLY):
    return FantasyProsECRArtifact(
        task_id="captask_" + "1" * 64,
        season=2026,
        week=8,
        horizon=horizon,
        scoring="PPR",
        position_scope=("ALL",),
        expert_ids=("expert-1", "expert-2"),
        expert_count=2,
        capture_method=ECRCaptureMethod.VISIBLE_PAGE,
        last_updated_text="Updated today",
        last_updated_at="2026-10-27T15:00:00Z",
        captured_at="2026-10-27T16:00:00Z",
        rankings=(
            ECRRankingRow(
                "101", "A.J. Brown", "PHI", "WR", 5, 2, 11, 5.5, 1.2,
                "WR 2", {"ECR": "5"},
            ),
        ),
    )


class CaptureNormalizeTests(unittest.TestCase):
    def test_ecr_artifact_builds_stable_identity_and_domain_snapshot(self):
        source = artifact()
        registry = reconcile_player_identities(ecr_provider_records(source))
        snapshot = ecr_snapshot_from_artifact(
            source,
            registry,
            snapshot_id="week-8",
            scoring_profile_id="ppr",
        )
        self.assertEqual(snapshot.period, EcrPeriod.WEEKLY)
        self.assertEqual(snapshot.rankings[0].canonical_player_id, "fantasypros:101")
        self.assertEqual(snapshot.rankings[0].position_rank, 2)
        self.assertEqual(snapshot.expert_ids, ("expert-1", "expert-2"))

    def test_ros_maps_period_and_unresolved_or_incomplete_rows_fail_closed(self):
        source = artifact(horizon=RankingHorizon.ROS)
        registry = reconcile_player_identities(ecr_provider_records(source))
        snapshot = ecr_snapshot_from_artifact(
            source,
            registry,
            snapshot_id="week-8",
            scoring_profile_id="ppr",
        )
        self.assertEqual(snapshot.period, EcrPeriod.REST_OF_SEASON)

        empty = reconcile_player_identities(
            ecr_provider_records(replace(source, rankings=(replace(source.rankings[0], provider_player_id="999"),)))
        )
        with self.assertRaisesRegex(ValueError, "unresolved"):
            ecr_snapshot_from_artifact(
                source,
                empty,
                snapshot_id="week-8",
                scoring_profile_id="ppr",
            )
        incomplete = replace(source.rankings[0], rank_std=None)
        broken = replace(source, rankings=(incomplete,))
        with self.assertRaisesRegex(ValueError, "missing required rank_std"):
            ecr_snapshot_from_artifact(
                broken,
                registry,
                snapshot_id="week-8",
                scoring_profile_id="ppr",
            )
        mismatched = replace(source.rankings[0], position_rank="RB 2")
        with self.assertRaisesRegex(ValueError, "does not match"):
            ecr_snapshot_from_artifact(
                replace(source, rankings=(mismatched,)),
                registry,
                snapshot_id="week-8",
                scoring_profile_id="ppr",
            )

    def test_projection_table_builds_exact_identity_weekly_points_stats_and_game_context(self):
        source = projection_artifact(
            CaptureProvider.ESPN,
            RankingHorizon.WEEKLY,
            "https://www.espn.com/nfl/player/_/id/202/aj-brown",
        )
        registry = reconcile_player_identities(
            (
                ProviderPlayerRecord("fantasypros", "101", "A.J. Brown", "WR", "PHI"),
                *projection_provider_records(source),
            )
        )
        evidence = projection_evidence_from_artifact(
            source,
            registry,
            snapshot_id="week-8",
            scoring_profile_id="ppr",
        )
        self.assertEqual(len(evidence), 1)
        row = evidence[0]
        self.assertIsInstance(row, WeeklyProjection)
        self.assertEqual(row.status, ProjectionStatus.OBSERVED)
        self.assertEqual(row.canonical_player_id, "fantasypros:101")
        self.assertEqual(row.projected_fantasy_points, 18.4)
        self.assertEqual(dict(row.raw_projected_stats), {"pass_yds": 12.0})
        self.assertEqual(row.nfl_game_id, "2026-W08-DAL-PHI")
        self.assertFalse(row.is_home)

    def test_ros_projection_uses_applicable_weeks_and_unresolved_is_explicit(self):
        source = projection_artifact(
            CaptureProvider.YAHOO,
            RankingHorizon.ROS,
            "https://sports.yahoo.com/nfl/players/303/",
        )
        records = projection_provider_records(source)
        registry = reconcile_player_identities(
            (ProviderPlayerRecord("fantasypros", "999", "Different Player", "WR", "PHI"),)
        )
        evidence = projection_evidence_from_artifact(
            source,
            registry,
            snapshot_id="week-8",
            scoring_profile_id="ppr",
            applicable_weeks=(8, 9, 10),
        )
        self.assertEqual(len(records), 1)
        self.assertIsInstance(evidence[0], RemainingSeasonProjection)
        self.assertEqual(evidence[0].status, ProjectionStatus.UNMATCHED_PLAYER)
        self.assertIsNone(evidence[0].canonical_player_id)
        self.assertEqual(evidence[0].applicable_weeks, (8, 9, 10))


def projection_artifact(provider, horizon, link):
    return GenericTableArtifact(
        task_id="captask_" + "2" * 64,
        provider=provider,
        season=2026,
        week=8,
        kind=CaptureKind.VISIBLE_TABLE,
        captured_at="2026-10-27T16:00:00Z",
        horizon=horizon,
        scoring="PPR",
        position_scope=("WR",),
        source_period_text="2026 | Week 8 | PPR | WR" if horizon is RankingHorizon.WEEKLY else "2026 | Rest of Season | PPR | WR",
        segments_captured=1,
        complete=True,
        tables=(
            VisibleTable(
                (
                    tuple(VisibleTableCell(value) for value in ("PLAYER", "TEAM", "POS", "FPTS", "PASS YDS", "OPP")),
                    (
                        VisibleTableCell("A.J. Brown", (link,)),
                        VisibleTableCell("PHI"),
                        VisibleTableCell("WR"),
                        VisibleTableCell("18.4"),
                        VisibleTableCell("12"),
                        VisibleTableCell("@DAL"),
                    ),
                )
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
