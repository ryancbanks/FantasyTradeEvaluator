import unittest

from tests.test_calibration_workflow import session
from trade_snapshot.analyzer_contract import BundleFingerprint, CURRENT_BUNDLE_FINGERPRINT
from trade_snapshot.calibration_capture import (
    build_calibration_capture_batch,
    observations_from_calibration_artifacts,
)
from trade_snapshot.capture_schema import AnalyzerResponseArtifact


CAPTURED = "2026-09-01T00:00:00Z"


class CalibrationCaptureTests(unittest.TestCase):
    def test_builds_only_queryless_ordinary_power_tasks(self):
        value = session()
        batch = build_calibration_capture_batch(value, season=2026, week=1)

        self.assertEqual(len(batch.plan.tasks), len(value.plan.experiments))
        self.assertEqual(
            {task.analyzer_phase.value for task in batch.plan.tasks},
            {"ordinary_power"},
        )
        self.assertTrue(all("?" not in task.url for task in batch.plan.tasks))
        self.assertEqual(
            set(batch.experiment_by_task_id.values()),
            {row.experiment_id for row in value.plan.experiments},
        )

    def test_parses_exact_artifact_coverage_into_session_observations(self):
        value = session()
        batch = build_calibration_capture_batch(value, season=2026, week=1)
        artifacts = []
        for task in batch.plan.tasks:
            request = batch.request_by_task_id[task.task_id]
            artifacts.append(
                AnalyzerResponseArtifact(
                    task.task_id,
                    "fantasypros",
                    2026,
                    1,
                    "analyzer_response",
                    CAPTURED,
                    "ordinary_power",
                    CURRENT_BUNDLE_FINGERPRINT.url,
                    CURRENT_BUNDLE_FINGERPRINT.sha256,
                    {
                        request.response_period_key: {
                            "powerRankings": {
                                "before": [
                                    {"teamId": request.team1_id, "score_decimal": 91.25},
                                    {"teamId": request.team2_id, "score_decimal": 88.75},
                                ],
                                "after": [
                                    {"teamId": request.team1_id, "score_decimal": 92.0},
                                    {"teamId": request.team2_id, "score_decimal": 89.0},
                                ],
                            }
                        }
                    },
                )
            )

        observations = observations_from_calibration_artifacts(
            batch,
            reversed(artifacts),
        )

        self.assertEqual(set(observations), set(value.requests))
        self.assertTrue(all(row.playoffs is None for row in observations.values()))
        self.assertTrue(
            all(row.bundle == CURRENT_BUNDLE_FINGERPRINT for row in observations.values())
        )
        with self.assertRaises(TypeError):
            observations["new"] = next(iter(observations.values()))
        with self.assertRaisesRegex(ValueError, "does not match"):
            observations_from_calibration_artifacts(
                batch, artifacts,
                bundle=BundleFingerprint(CURRENT_BUNDLE_FINGERPRINT.url, "0" * 64),
            )

    def test_rejects_partial_or_duplicate_artifact_coverage(self):
        batch = build_calibration_capture_batch(session(), season=2026, week=1)
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            observations_from_calibration_artifacts(
                batch,
                (),
                bundle=CURRENT_BUNDLE_FINGERPRINT,
            )


if __name__ == "__main__":
    unittest.main()
