import copy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_calibration_fit import corpus, exact_corpus, fit
from tests.test_feature_engineering import (
    full_horizon_evidence,
    grid_feature_kwargs,
    inputs,
)
from trade_snapshot.feature_engineering import build_strength_features
from trade_snapshot.projections import RemainingSeasonProjection
from trade_snapshot.strength import CalibrationStatus, RoleDefinition, RoleKind
from trade_snapshot.strength_calibration import CalibrationMetadata
from trade_snapshot.strength_formula import (
    StrengthFormula,
    load_strength_formula,
    save_strength_formula,
)
from trade_snapshot.trade_space import TeamRoster


def metadata():
    return CalibrationMetadata(
        "https://www.fantasypros.com/assets/app.js",
        "a" * 64,
        "b" * 64,
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        CalibrationStatus.UNVALIDATED,
    )


def formula(scoring_profile_id="profile-1"):
    role = RoleDefinition("RB_START_1", RoleKind.STARTER, "RB", frozenset({"RB"}))
    return StrengthFormula(
        source_fit_id="strength-fit-v1-test",
        trained_snapshot_id="calibration-week",
        season=2026,
        scoring_profile_id=scoring_profile_id,
        role_definitions=(role,),
        residual_weights={"presence": 0.5},
        role_weights={"RB_START_1": {"projection_fantasypros_full_ros_points": 1.0}},
        calibration=metadata(),
    )


class StrengthFormulaTests(unittest.TestCase):
    def test_fitted_methodology_has_strict_portable_round_trip(self):
        fitted = fit(corpus())
        value = StrengthFormula.from_fitted(fitted)
        self.assertEqual(
            value.held_out_trade_ids,
            tuple(row.trade_id for row in fitted.corpus.held_out_trades),
        )
        self.assertEqual(value.held_out_balanced_package_sizes, (1,))
        self.assertEqual(value.to_record()["schema_version"], 2)
        self.assertEqual(StrengthFormula.from_record(value.to_record()), value)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "formula.json"
            self.assertEqual(save_strength_formula(value, path), path.resolve())
            self.assertEqual(load_strength_formula(path), value)
        tampered = copy.deepcopy(value.to_record())
        tampered["residual_weights"]["value"] += 1
        with self.assertRaisesRegex(ValueError, "does not match formula_id"):
            StrengthFormula.from_record(tampered)
        legacy = value.to_record()
        legacy["schema_version"] = 1
        with self.assertRaisesRegex(ValueError, "schema version"):
            StrengthFormula.from_record(legacy)

    def test_exact_formula_rejects_atomic_only_blind_scope(self):
        with self.assertRaisesRegex(ValueError, "2-for-2, 3-for-3, and 4-for-4"):
            StrengthFormula.from_fitted(fit(exact_corpus()))

    def test_rebuilds_current_player_scores_and_weekly_denominator_locally(self):
        snapshots, projections, eligibility = inputs()
        features = build_strength_features(
            snapshots, projections, eligibility, **grid_feature_kwargs(projections)
        )
        model = formula().build_model(
            features,
            (
                TeamRoster("a", ("p1",), 1, 1),
                TeamRoster("b", ("p2",), 1, 1),
            ),
        )
        self.assertEqual(model.snapshot_id, "snapshot-1")
        self.assertEqual(model.players["p1"].residual_score, 0.5)
        self.assertEqual(model.players["p1"].assignment_score_by_role["RB_START_1"], 30)
        self.assertEqual(model.normalization_denominator, 30.5)
        self.assertEqual(model.score_roster(("p1",)).power_score, 100)
        self.assertLess(model.score_roster(("p2",)).power_score, 100)

    def test_rejects_identity_drift_missing_inputs_and_shared_players(self):
        snapshots, projections, eligibility = inputs()
        features = build_strength_features(
            snapshots, projections, eligibility, **grid_feature_kwargs(projections)
        )
        rosters = (
            TeamRoster("a", ("p1",), 1, 1),
            TeamRoster("b", ("p2",), 1, 1),
        )
        with self.assertRaisesRegex(ValueError, "season and scoring profile"):
            formula().build_model(replace(features, season=2027), rosters)
        bad = StrengthFormula(
            "fit", "old", 2026, "profile-1", formula().role_definitions,
            {"not_captured": 1}, formula().role_weights, metadata(),
        )
        with self.assertRaisesRegex(ValueError, "missing formula input"):
            bad.build_model(features, rosters)
        with self.assertRaisesRegex(ValueError, "share player"):
            formula().build_model(
                features,
                (
                    TeamRoster("a", ("p1",), 1, 1),
                    TeamRoster("b", ("p1",), 1, 1),
                ),
            )

    def test_requires_only_the_full_horizon_sources_used_by_the_formula(self):
        snapshots, projections, eligibility = inputs()
        rosters = (
            TeamRoster("a", ("p1",), 1, 1),
            TeamRoster("b", ("p2",), 1, 1),
        )

        def without(provider):
            return tuple(
                row
                for row in full_horizon_evidence(projections)
                if not (
                    isinstance(row, RemainingSeasonProjection)
                    and row.canonical_player_id == "p1"
                    and row.provider == provider
                )
            )

        optional_gap = build_strength_features(
            snapshots,
            projections,
            eligibility,
            projection_evidence=without("yahoo"),
            remaining_week_scopes={"p1": (1, 2, 3, 4), "p2": (1, 2, 3, 4)},
        )
        formula().build_model(optional_gap, rosters)

        required_gap = build_strength_features(
            snapshots,
            projections,
            eligibility,
            projection_evidence=without("fantasypros"),
            remaining_week_scopes={"p1": (1, 2, 3, 4), "p2": (1, 2, 3, 4)},
        )
        with self.assertRaisesRegex(ValueError, "required feature.*p1"):
            formula().build_model(required_gap, rosters)

    def test_rejects_non_fantasypros_projection_features(self):
        for feature in (
            "projection_espn_full_ros_points",
            "projection_yahoo_full_ros_points",
            "projection_ensemble_full_ros_points",
            "projection_sleeper_full_ros_points",
        ):
            with self.subTest(feature=feature):
                with self.assertRaisesRegex(ValueError, "FantasyPros projection features"):
                    StrengthFormula(
                        "fit",
                        "old",
                        2026,
                        "profile-1",
                        formula().role_definitions,
                        {"presence": 1},
                        {"RB_START_1": {feature: 1}},
                        metadata(),
                    )


if __name__ == "__main__":
    unittest.main()
