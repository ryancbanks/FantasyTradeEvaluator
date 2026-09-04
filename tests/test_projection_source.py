import unittest
from dataclasses import replace

from tests.test_capture_normalize import projection_artifact
from trade_snapshot.capture_normalize import (
    projection_evidence_from_artifact,
    projection_provider_records,
)
from trade_snapshot.capture_schema import (
    CaptureProvider,
    RankingHorizon,
    VisibleTable,
    VisibleTableCell,
)
from trade_snapshot.identity_match import (
    ProviderPlayerRecord,
    reconcile_player_identities,
)
from trade_snapshot.projection_source import (
    HostScoringCompatibility,
    ProjectionAttemptReason,
    ProjectionAttemptStatus,
    ProjectionInputPresence,
    ProjectionPointBasis,
    ProjectionSourceAttempt,
    ProjectionSourceManifest,
    projection_input_id,
)
from trade_snapshot.projections import ProjectionStatus


class ProjectionSourceManifestTests(unittest.TestCase):
    def setUp(self):
        self.artifact = projection_artifact(
            CaptureProvider.ESPN,
            RankingHorizon.WEEKLY,
            "https://www.espn.com/nfl/player/_/id/202/aj-brown",
        )
        registry = reconcile_player_identities(
            (
                ProviderPlayerRecord(
                    "fantasypros", "101", "A.J. Brown", "WR", "PHI"
                ),
                *projection_provider_records(self.artifact),
            )
        )
        self.evidence = projection_evidence_from_artifact(
            self.artifact,
            registry,
            snapshot_id="week-8",
            scoring_profile_id="ppr",
        )

    def test_binds_exact_normalized_input_and_round_trips(self):
        manifest = ProjectionSourceManifest.from_artifacts(
            (self.artifact,), self.evidence
        )

        self.assertEqual(len(manifest.sources), 1)
        source = manifest.sources[0]
        self.assertEqual(source.artifact_id, self.artifact.artifact_id)
        self.assertEqual(source.provider, CaptureProvider.ESPN)
        self.assertEqual(source.horizon, RankingHorizon.WEEKLY)
        self.assertEqual(source.week, 8)
        self.assertEqual(source.source_scoring_format, "PPR")
        self.assertEqual(source.point_basis.value, "provider_total")
        self.assertEqual(source.host_scoring_compatibility.value, "base_format_only")
        self.assertEqual(manifest.evaluation_scoring_profile_id, "ppr")
        self.assertEqual(source.position_scope, ("WR",))
        self.assertEqual(source.source_period_text, self.artifact.source_period_text)
        self.assertEqual(manifest.attempts[0].attempted_at, source.captured_at)
        self.assertEqual(
            source.inputs[0].projection_input_id,
            projection_input_id(self.evidence[0]),
        )
        self.assertIs(source.inputs[0].presence, ProjectionInputPresence.SOURCE_ROW)
        self.assertEqual(manifest.to_record()["schema_version"], 2)
        self.assertEqual(
            ProjectionSourceManifest.from_record(manifest.to_record()), manifest
        )

    def test_distinguishes_present_empty_row_from_complete_capture_omission(self):
        table = self.artifact.tables[0]
        cells = list(table.rows[1])
        cells[3] = VisibleTableCell("")
        present_empty_artifact = replace(
            self.artifact,
            tables=(VisibleTable((table.rows[0], tuple(cells))),),
        )
        registry = reconcile_player_identities(
            (
                ProviderPlayerRecord(
                    "fantasypros", "101", "A.J. Brown", "WR", "PHI"
                ),
                ProviderPlayerRecord(
                    "fantasypros", "102", "Missing Receiver", "WR", "DAL"
                ),
                *projection_provider_records(present_empty_artifact),
                ProviderPlayerRecord(
                    "espn", "302", "Missing Receiver", "WR", "DAL"
                ),
            )
        )
        evidence = projection_evidence_from_artifact(
            present_empty_artifact,
            registry,
            snapshot_id="week-8",
            scoring_profile_id="ppr",
        )

        manifest = ProjectionSourceManifest.from_artifacts(
            (present_empty_artifact,), evidence, identities=registry
        )
        presence = {
            row.canonical_player_id: row.presence
            for row in manifest.sources[0].inputs
        }

        self.assertEqual(
            {row.status for row in evidence}, {ProjectionStatus.NOT_PUBLISHED}
        )
        self.assertIs(
            presence["fantasypros:101"], ProjectionInputPresence.SOURCE_ROW
        )
        self.assertIs(
            presence["fantasypros:102"],
            ProjectionInputPresence.OMITTED_FROM_COMPLETE_CAPTURE,
        )
        with self.assertRaisesRegex(ValueError, "exactly one raw artifact"):
            ProjectionSourceManifest.from_artifacts(
                (present_empty_artifact,), evidence
            )

    def test_rejects_changed_normalized_value_and_tampered_manifest(self):
        manifest = ProjectionSourceManifest.from_artifacts(
            (self.artifact,), self.evidence
        )
        changed = (replace(self.evidence[0], projected_fantasy_points=99.0),)
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            manifest.validate_projection_evidence(changed)

        record = manifest.to_record()
        record["sources"][0]["source_period_text"] = "changed"
        with self.assertRaisesRegex(ValueError, "manifest_id"):
            ProjectionSourceManifest.from_record(record)

    def test_rejects_unproven_exact_host_recomputation_claim(self):
        source = ProjectionSourceManifest.from_artifacts(
            (self.artifact,), self.evidence
        ).sources[0]

        with self.assertRaisesRegex(ValueError, "proof-bound recomputation artifact"):
            replace(
                source,
                point_basis=ProjectionPointBasis.LOCALLY_RECOMPUTED,
                host_scoring_compatibility=HostScoringCompatibility.EXACT_HOST_RULES,
            )

    def test_retains_unavailable_attempt_without_fabricating_an_artifact(self):
        captured = ProjectionSourceAttempt(
            self.artifact.task_id,
            self.artifact.provider,
            self.artifact.season,
            self.artifact.week,
            self.artifact.horizon,
            self.artifact.scoring,
            self.artifact.position_scope,
            self.evidence[0].captured_at,
            ProjectionAttemptStatus.CAPTURED,
            ProjectionAttemptReason.CAPTURED,
            self.artifact.artifact_id,
        )
        unavailable = ProjectionSourceAttempt(
            "captask_" + "3" * 64,
            "yahoo",
            2026,
            8,
            "ros",
            "PPR",
            ("WR",),
            self.evidence[0].captured_at,
            "unavailable",
            "provider_layout_unsupported",
        )

        manifest = ProjectionSourceManifest.from_artifacts(
            (self.artifact,), self.evidence, attempts=(captured, unavailable)
        )

        self.assertEqual(
            {row.status for row in manifest.attempts},
            {ProjectionAttemptStatus.CAPTURED, ProjectionAttemptStatus.UNAVAILABLE},
        )
        self.assertIsNone(
            next(
                row for row in manifest.attempts
                if row.status is ProjectionAttemptStatus.UNAVAILABLE
            ).artifact_id
        )
        self.assertEqual(
            next(
                row for row in manifest.attempts
                if row.status is ProjectionAttemptStatus.UNAVAILABLE
            ).attempted_at,
            self.evidence[0].captured_at,
        )

    def test_attempt_status_reason_and_success_coverage_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ProjectionSourceAttempt(
                "captask_" + "4" * 64,
                "espn",
                2026,
                8,
                "weekly",
                "PPR",
                ("ALL",),
                self.evidence[0].captured_at.replace(tzinfo=None),
                "unavailable",
                "provider_page_unavailable",
            )
        with self.assertRaisesRegex(ValueError, "status and reason"):
            ProjectionSourceAttempt(
                "captask_" + "4" * 64,
                "espn",
                2026,
                8,
                "weekly",
                "PPR",
                ("ALL",),
                self.evidence[0].captured_at,
                "unavailable",
                "source_not_published",
            )
        missing = ProjectionSourceAttempt(
            "captask_" + "5" * 64,
            "yahoo",
            2026,
            8,
            "weekly",
            "PPR",
            ("WR",),
            self.evidence[0].captured_at,
            "unavailable",
            "provider_page_unavailable",
        )
        with self.assertRaisesRegex(ValueError, "exactly cover raw artifacts"):
            ProjectionSourceManifest.from_artifacts(
                (self.artifact,), self.evidence, attempts=(missing,)
            )


if __name__ == "__main__":
    unittest.main()
