import unittest

from trade_snapshot.calibration_plan import (
    CalibrationExperimentPurpose,
    design_calibration_experiments,
)
from trade_snapshot.feature_engineering import StrengthFeatureSet, feature_names
from trade_snapshot.calibration_fit import PlayerFeatureVector
from trade_snapshot.strength import RoleDefinition, RoleKind
from trade_snapshot.trade_space import TeamRoster


def features():
    names = feature_names(("fantasypros",))
    rows = tuple(
        PlayerFeatureVector(
            player_id,
            frozenset({position}),
            {
                name: (
                    1.0
                    if name.endswith("_available")
                    else value
                    if name == "ecr_ros_inverse_rank"
                    else value * value + team_offset
                    if name == "projection_fantasypros_full_ros_points"
                    else 0.0
                )
                for name in names
            },
        )
        for team_offset, values in enumerate(
            (
                (("a1", "RB", 1.0), ("a2", "WR", 2.0), ("a3", "RB", 3.0)),
                (("b1", "RB", 4.0), ("b2", "WR", 5.0), ("b3", "RB", 6.0)),
                (("c1", "RB", 7.0), ("c2", "WR", 8.0), ("c3", "RB", 9.0)),
            )
        )
        for player_id, position, value in values
    )
    return StrengthFeatureSet(
        "week-1", "ppr", 2026, 1, (1,), ("fantasypros",),
        ("weekly-ecr", "ros-ecr"), rows,
    )


def production_size_inputs():
    names = feature_names(("fantasypros",))
    rows = []
    rosters = []
    for team_offset, team_id in enumerate(("a", "b", "c", "d")):
        player_ids = []
        for index in range(14):
            player_id = f"{team_id}{index:02d}"
            player_ids.append(player_id)
            position = "RB" if index % 2 == 0 else "WR"
            value = float(team_offset * 100 + index + 1)
            rows.append(
                PlayerFeatureVector(
                    player_id,
                    frozenset({position}),
                    {
                        name: (
                            1.0
                            if name.endswith("_available")
                            else value
                            if name == "ecr_ros_inverse_rank"
                            else value * value + index * index * index / 1000
                            if name == "projection_fantasypros_full_ros_points"
                            else 0.0
                        )
                        for name in names
                    },
                )
            )
        rosters.append(TeamRoster(team_id, tuple(player_ids), 14, 14))
    return (
        StrengthFeatureSet(
            "week-production",
            "ppr",
            2026,
            1,
            (1,),
            ("fantasypros",),
            ("weekly-ecr", "ros-ecr"),
            tuple(rows),
        ),
        tuple(rosters),
    )


class CalibrationPlanTests(unittest.TestCase):
    def test_selects_small_diverse_balanced_training_and_holdout_plan(self):
        roles = (
            RoleDefinition("RB", RoleKind.STARTER, "RB", frozenset({"RB"})),
            RoleDefinition("WR", RoleKind.STARTER, "WR", frozenset({"WR"})),
        )
        rosters = (
            TeamRoster("a", ("a1", "a2", "a3"), 3, 3),
            TeamRoster("b", ("b1", "b2", "b3"), 3, 3),
            TeamRoster("c", ("c1", "c2", "c3"), 3, 3),
        )
        plan = design_calibration_experiments(
            features(), roles, rosters,
            primary_team_id="a",
            residual_feature_names=("ecr_ros_inverse_rank",),
            role_feature_names=("projection_fantasypros_full_ros_points",),
            training_experiment_count=3,
            held_out_experiment_count=2,
        )
        self.assertEqual(len(plan.training), 3)
        self.assertEqual(len(plan.held_out), 2)
        self.assertEqual({row.team2_id for row in plan.experiments}, {"b", "c"})
        self.assertEqual(len({row.design_signature for row in plan.experiments}), 5)
        self.assertTrue(
            all(len(row.team1_gives) == len(row.team2_gives) for row in plan.experiments)
        )
        self.assertTrue(
            all(len(row.team1_gives) == 1 for row in plan.training)
        )
        self.assertIn(2, plan.held_out_balanced_package_sizes)
        self.assertTrue(
            all(row.purpose is CalibrationExperimentPurpose.TRAINING for row in plan.training)
        )

    def test_production_budget_blindly_covers_every_nonleaking_balanced_size(self):
        role_rows = (
            RoleDefinition("RB", RoleKind.STARTER, "RB", frozenset({"RB"})),
            RoleDefinition("WR", RoleKind.STARTER, "WR", frozenset({"WR"})),
        )
        feature_rows, rosters = production_size_inputs()
        first = design_calibration_experiments(
            feature_rows,
            role_rows,
            rosters,
            primary_team_id="a",
            residual_feature_names=("ecr_ros_inverse_rank",),
            role_feature_names=("projection_fantasypros_full_ros_points",),
        )
        second = design_calibration_experiments(
            feature_rows,
            role_rows,
            tuple(reversed(rosters)),
            primary_team_id="a",
            residual_feature_names=("ecr_ros_inverse_rank",),
            role_feature_names=("projection_fantasypros_full_ros_points",),
        )
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(len(first.training), 250)
        self.assertEqual(len(first.held_out), 100)
        self.assertTrue(all(len(row.team1_gives) == 1 for row in first.training))
        self.assertEqual(
            first.held_out_balanced_package_sizes, tuple(range(1, 14))
        )
        self.assertTrue(
            {2, 3, 4}.issubset(first.held_out_balanced_package_sizes)
        )
        self.assertEqual({row.team2_id for row in first.held_out}, {"b", "c", "d"})

    def test_fails_when_requested_distinct_evidence_exceeds_available_swaps(self):
        roles = (RoleDefinition("RB", RoleKind.STARTER, "RB", frozenset({"RB"})),)
        rosters = (
            TeamRoster("a", ("a1", "a2", "a3"), 3, 3),
            TeamRoster("b", ("b1", "b2", "b3"), 3, 3),
            TeamRoster("c", ("c1", "c2", "c3"), 3, 3),
        )
        with self.assertRaisesRegex(ValueError, "distinct one-player training"):
            design_calibration_experiments(
                features(), roles, rosters,
                primary_team_id="a",
                residual_feature_names=("ecr_ros_inverse_rank",),
                role_feature_names=("projection_fantasypros_full_ros_points",),
                training_experiment_count=100,
                held_out_experiment_count=100,
            )


if __name__ == "__main__":
    unittest.main()
