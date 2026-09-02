import unittest

from trade_snapshot.analyzer_contract import (
    AnalyzerObservation,
    AnalyzerPeriod,
    CURRENT_BUNDLE_FINGERPRINT,
    PowerRankingChange,
    PowerRankingObservation,
)
from trade_snapshot.calibration_observations import (
    analyzer_request_for_experiment,
    prepare_calibration_evidence,
)
from trade_snapshot.calibration_plan import design_calibration_experiments
from trade_snapshot.strength import RoleDefinition, RoleKind
from trade_snapshot.trade_space import TeamRoster
from tests.test_calibration_plan import features


ROSTERS = (
    TeamRoster("a", ("a1", "a2", "a3"), 3, 3),
    TeamRoster("b", ("b1", "b2", "b3"), 3, 3),
    TeamRoster("c", ("c1", "c2", "c3"), 3, 3),
)
ROLES = (
    RoleDefinition("RB", RoleKind.STARTER, "RB", frozenset({"RB"})),
    RoleDefinition("WR", RoleKind.STARTER, "WR", frozenset({"WR"})),
)
TEAM_IDS = {"a": "1", "b": "2", "c": "3"}
PLAYER_IDS = {player: str(index) for index, player in enumerate(
    ("a1", "a2", "a3", "b1", "b2", "b3", "c1", "c2", "c3"), 101
)}


def plan():
    return design_calibration_experiments(
        features(), ROLES, ROSTERS,
        primary_team_id="a",
        residual_feature_names=("ecr_ros_inverse_rank",),
        role_feature_names=("projection_fantasypros_remaining_points",),
        training_experiment_count=3,
        held_out_experiment_count=2,
    )


def observations(value):
    baseline = {"1": 100.0, "2": 90.0, "3": 80.0}
    result = {}
    for index, experiment in enumerate(value.experiments):
        request = analyzer_request_for_experiment(
            experiment, team_provider_ids=TEAM_IDS, player_provider_ids=PLAYER_IDS
        )
        first, second = request.team1_id, request.team2_id
        power = PowerRankingObservation(
            AnalyzerPeriod.ROS,
            "ros",
            PowerRankingChange(first, baseline[first], baseline[first] + index + 1),
            PowerRankingChange(second, baseline[second], baseline[second] - index - 1),
        )
        result[experiment.experiment_id] = AnalyzerObservation(
            request, power, bundle=CURRENT_BUNDLE_FINGERPRINT
        )
    return result


class CalibrationObservationTests(unittest.TestCase):
    def test_prepares_canonical_training_and_leakage_safe_holdouts(self):
        value = plan()
        prepared = prepare_calibration_evidence(
            value,
            observations(value),
            features(),
            ROLES,
            ROSTERS,
            team_provider_ids=TEAM_IDS,
            player_provider_ids=PLAYER_IDS,
        )
        self.assertEqual(prepared.bundle, CURRENT_BUNDLE_FINGERPRINT)
        self.assertEqual(len(prepared.corpus.samples), 3 + 2 * len(value.training))
        self.assertEqual(len(prepared.corpus.held_out_trades), len(value.held_out))
        self.assertTrue(prepared.evidence_id.startswith("prepared-calibration-evidence_"))

    def test_rejects_request_mismatch_or_changing_baseline(self):
        value = plan()
        rows = observations(value)
        first = value.experiments[0].experiment_id
        second = value.experiments[1].experiment_id
        rows[first] = rows[second]
        with self.assertRaisesRegex(ValueError, "does not match experiment"):
            prepare_calibration_evidence(
                value, rows, features(), ROLES, ROSTERS,
                team_provider_ids=TEAM_IDS, player_provider_ids=PLAYER_IDS,
            )

        rows = observations(value)
        observation = rows[second]
        changed_power = PowerRankingObservation(
            observation.power.semantic_period,
            observation.power.response_period_key,
            PowerRankingChange(
                observation.power.team1.team_id,
                observation.power.team1.raw_before + 1,
                observation.power.team1.raw_after,
            ),
            observation.power.team2,
        )
        rows[second] = AnalyzerObservation(
            observation.request, changed_power, bundle=observation.bundle
        )
        with self.assertRaisesRegex(ValueError, "baseline power changed"):
            prepare_calibration_evidence(
                value, rows, features(), ROLES, ROSTERS,
                team_provider_ids=TEAM_IDS, player_provider_ids=PLAYER_IDS,
            )


if __name__ == "__main__":
    unittest.main()
