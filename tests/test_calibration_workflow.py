from datetime import datetime, timezone
import unittest

from tests.test_calibration_observations import (
    PLAYER_IDS,
    ROLES,
    ROSTERS,
    TEAM_IDS,
    observations,
)
from tests.test_calibration_plan import features
from trade_snapshot.analyzer_contract import CURRENT_BUNDLE_FINGERPRINT
from trade_snapshot.analyzer_contract import (
    AnalyzerObservation,
    AnalyzerPeriod,
    PowerRankingChange,
    PowerRankingObservation,
)
from trade_snapshot.calibration_workflow import (
    CalibrationNotExact,
    complete_calibration_session,
    prepare_calibration_session,
)
from trade_snapshot.methodology import PowerMethodology
from trade_snapshot.methodology_reuse import MethodologyFingerprint
from trade_snapshot.formula_verification import (
    verification_report_from_calibration_session,
)
from trade_snapshot.strength import CalibrationStatus
from trade_snapshot.strength_calibration import CalibrationMetadata
from trade_snapshot.strength_formula import StrengthFormula


METHODOLOGY = PowerMethodology(
    ("ecr_ros_inverse_rank",),
    ("projection_fantasypros_remaining_points",),
)


def session():
    return prepare_calibration_session(
        features=features(),
        roles=ROLES,
        rosters=ROSTERS,
        primary_team_id="a",
        methodology=METHODOLOGY,
        fingerprint=MethodologyFingerprint(
            CURRENT_BUNDLE_FINGERPRINT,
            "a" * 64,
            METHODOLOGY,
            ROLES,
        ),
        team_provider_ids=TEAM_IDS,
        player_provider_ids=PLAYER_IDS,
        training_experiment_count=3,
        held_out_experiment_count=2,
    )


def exact_formula(value):
    return StrengthFormula(
        source_fit_id="test-fit",
        trained_snapshot_id=value.features.snapshot_id,
        season=value.features.season,
        scoring_profile_id=value.features.scoring_profile_id,
        role_definitions=value.roles,
        residual_weights={"ecr_ros_inverse_rank": 0.25},
        role_weights={
            role.role_id: {"projection_fantasypros_remaining_points": 0.75}
            for role in value.roles
        },
        calibration=CalibrationMetadata(
            CURRENT_BUNDLE_FINGERPRINT.url,
            CURRENT_BUNDLE_FINGERPRINT.sha256,
            "a" * 64,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            CalibrationStatus.UNVALIDATED,
        ),
    )


def exact_observations(value, formula):
    model = formula.build_model(value.features, value.rosters)
    rosters = {row.team_id: row.player_ids for row in value.rosters}
    result = {}
    for experiment in value.plan.experiments:
        before1 = rosters[experiment.team1_id]
        before2 = rosters[experiment.team2_id]
        after1 = tuple(sorted(
            set(before1).difference(experiment.team1_gives).union(experiment.team2_gives)
        ))
        after2 = tuple(sorted(
            set(before2).difference(experiment.team2_gives).union(experiment.team1_gives)
        ))
        request = value.requests[experiment.experiment_id]
        result[experiment.experiment_id] = AnalyzerObservation(
            request,
            PowerRankingObservation(
                AnalyzerPeriod.ROS,
                "ros",
                PowerRankingChange(
                    request.team1_id,
                    model.score_roster(before1).power_score,
                    model.score_roster(after1).power_score,
                ),
                PowerRankingChange(
                    request.team2_id,
                    model.score_roster(before2).power_score,
                    model.score_roster(after2).power_score,
                ),
            ),
            bundle=CURRENT_BUNDLE_FINGERPRINT,
        )
    return result


class CalibrationWorkflowTests(unittest.TestCase):
    def test_prepares_exact_bounded_request_set(self):
        value = session()
        self.assertEqual(len(value.requests), 5)
        self.assertEqual(set(value.requests), {
            row.experiment_id for row in value.plan.experiments
        })
        self.assertTrue(value.session_id.startswith("calibration-session_"))
        with self.assertRaises(TypeError):
            value.requests["new"] = next(iter(value.requests.values()))

    def test_refuses_to_publish_a_nonexact_surrogate(self):
        value = session()
        with self.assertRaises(CalibrationNotExact) as raised:
            complete_calibration_session(
                value,
                observations(value.plan),
                captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(raised.exception.diagnostics.held_out_trade_count, 2)
        self.assertEqual(
            raised.exception.candidate_formula.calibration.status,
            CalibrationStatus.SURROGATE,
        )
        self.assertFalse(raised.exception.surrogate_eligible)
        with self.assertRaises(CalibrationNotExact):
            complete_calibration_session(
                value,
                observations(value.plan),
                captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                allow_surrogate_power=True,
            )

    def test_builds_weekly_reuse_report_from_fresh_blind_observations(self):
        value = session()
        formula = exact_formula(value)
        report = verification_report_from_calibration_session(
            value,
            exact_observations(value, formula),
            formula,
            weekly_snapshot_id=value.features.snapshot_id,
            verified_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(report.formula_id, formula.formula_id)
        self.assertEqual(len(report.ordinary_power_holdout_ids), 2)
        self.assertLessEqual(report.max_absolute_score_error, 1e-12)
        self.assertLessEqual(report.max_absolute_delta_error, 1e-12)
        self.assertEqual(report.display_match_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
