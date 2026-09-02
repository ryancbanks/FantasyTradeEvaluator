from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_feature_engineering import (
    inputs,
    projection as feature_projection,
    rank as ecr_rank,
)
from tests.test_methodology_reuse import SCHEMA, formula, fingerprint
from tests.test_weekly_engine import (
    FORECAST_PROVIDERS,
    SCORING_PROFILE,
    nfl_schedule,
    raw_rows,
    state,
    waiver_pool,
)
from trade_snapshot.ensemble import EnsembleConfig, ProviderWeight
from trade_snapshot.formula_verification import FormulaVerificationReport
from trade_snapshot.methodology import DEFAULT_POWER_METHODOLOGY
from trade_snapshot.methodology_reuse import FormulaAction
from trade_snapshot.scenario_config import (
    CorrelatedScenarioConfig,
    FactorLoadings,
    PlayerEligibility,
)
from trade_snapshot.scoring import ScoringProfile
from trade_snapshot.strength import CalibrationStatus
from trade_snapshot.strength_formula import load_strength_formula, save_strength_formula
from trade_snapshot.trade_space import TeamRoster
from trade_snapshot.weekly_refresh import (
    CalibrationRequired,
    RefreshCancelled,
    RefreshStage,
    WeeklyRefreshEvidence,
    refresh_weekly_engine,
)


def evidence():
    profile_id = SCORING_PROFILE.scoring_profile_id
    ecr, ensembles, eligibility = inputs(profile_id)
    ecr = tuple(
        replace(
            snapshot,
            rankings=(
                *snapshot.rankings,
                ecr_rank("p3", "303", 3, 3),
                ecr_rank("p4", "304", 4, 4),
            ),
        )
        for snapshot in ecr
    )
    ensembles = (
        *ensembles,
        replace(feature_projection("p3", 1, 4, scoring_profile_id=profile_id), nfl_team_id="NFL-P3"),
        replace(feature_projection("p3", 2, 4, scoring_profile_id=profile_id), nfl_team_id="NFL-P3"),
        replace(feature_projection("p4", 1, 3, scoring_profile_id=profile_id), nfl_team_id="NFL-P4"),
        replace(feature_projection("p4", 2, 3, scoring_profile_id=profile_id), nfl_team_id="NFL-P4"),
    )
    eligibility = (
        *eligibility,
        PlayerEligibility("p3", ("RB", "FLEX")),
        PlayerEligibility("p4", ("RB", "FLEX")),
    )
    method = fingerprint()
    return WeeklyRefreshEvidence(
        state=state(profile_id),
        scoring_profile=SCORING_PROFILE,
        rosters=(
            TeamRoster("a", ("p1",), 1, 2),
            TeamRoster("b", ("p2",), 1, 2),
        ),
        projection_evidence=raw_rows(ensembles),
        nfl_schedule=nfl_schedule(),
        ecr_snapshots=ecr,
        eligibilities=eligibility,
        player_positions={"p1": "RB", "p2": "RB", "p3": "RB", "p4": "RB"},
        player_nfl_team_ids={
            "p1": "NFL-p1",
            "p2": "NFL-p2",
            "p3": "NFL-P3",
            "p4": "NFL-P4",
        },
        player_names={
            "p1": "Player One",
            "p2": "Player Two",
            "p3": "Player Three",
            "p4": "Player Four",
        },
        ensemble_config=EnsembleConfig(
            tuple(
                ProviderWeight(provider, 1)
                for provider in FORECAST_PROVIDERS
            ),
            2,
            {"RB": 0},
        ),
        scenario_config=CorrelatedScenarioConfig(
            100, 7, FactorLoadings(0, 0, 0, 1)
        ),
        analyzer_bundle=method.analyzer_bundle,
        response_schema_sha256=SCHEMA,
        power_methodology=DEFAULT_POWER_METHODOLOGY,
        role_definitions=(refresh_formula().role_definitions[0],),
        waiver_pool=waiver_pool(profile_id),
    )


def refresh_formula():
    return formula(scoring_profile_id=SCORING_PROFILE.scoring_profile_id)


def surrogate_formula():
    exact = refresh_formula()
    return replace(
        exact,
        calibration=replace(
            exact.calibration,
            status=CalibrationStatus.SURROGATE,
            max_absolute_score_error=0.2,
            display_match_rate=0.95,
        ),
    )


def verification_report(value=None, **changes):
    current = value or evidence()
    values = {
        "formula_id": refresh_formula().formula_id,
        "methodology_fingerprint_id": current.methodology_fingerprint.fingerprint_id,
        "weekly_snapshot_id": current.state.snapshot_id,
        "ordinary_power_holdout_ids": tuple(
            f"ordinary-power-{index}" for index in range(100)
        ),
        "balanced_package_sizes": (1, 2, 3, 4),
        "max_absolute_score_error": 1e-6,
        "max_absolute_delta_error": 1e-6,
        "display_match_rate": 1.0,
        "verified_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }
    values.update(changes)
    return FormulaVerificationReport(**values)


class WeeklyRefreshTests(unittest.TestCase):
    def test_evidence_requires_the_exact_scoring_profile(self):
        with self.assertRaisesRegex(ValueError, "scoring profile"):
            replace(
                evidence(),
                scoring_profile=ScoringProfile("espn", {"reception": 0}),
            )

    def test_reuses_formula_only_after_weekly_verification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula_path = root / "formula.json"
            save_strength_formula(refresh_formula(), formula_path)
            stages = []
            verifier_calls = []
            current = evidence()

            def forbidden(*_):
                raise AssertionError("calibrator must not run for ordinary weekly refresh")

            def verify(refresh_evidence, saved_formula, method):
                verifier_calls.append((refresh_evidence, saved_formula, method))
                return verification_report(current)

            result = refresh_weekly_engine(
                current,
                formula_path=formula_path,
                bundle_directory=root / "bundles",
                calibrate=forbidden,
                verify_reuse=verify,
                progress=lambda row: stages.append(row.stage),
            )
            self.assertEqual(len(verifier_calls), 1)
            self.assertEqual(result.formula_decision.action, FormulaAction.REUSE)
            self.assertEqual(result.reuse_verification, verification_report(current))
            self.assertEqual(
                result.bundle.methodology_attestation.formula_decision.action,
                FormulaAction.REUSE,
            )
            self.assertEqual(
                result.bundle.methodology_attestation.reuse_verification,
                verification_report(current),
            )
            self.assertTrue(result.bundle_path.is_file())
            self.assertIn(RefreshStage.VERIFYING_FORMULA, stages)
            self.assertIn(RefreshStage.REUSING_FORMULA, stages)
            self.assertEqual(stages[-1], RefreshStage.COMPLETE)

    def test_missing_or_failed_verification_routes_to_recalibration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula_path = root / "formula.json"
            save_strength_formula(refresh_formula(), formula_path)
            current = evidence()
            with self.assertRaises(CalibrationRequired) as raised:
                refresh_weekly_engine(
                    current,
                    formula_path=formula_path,
                    bundle_directory=root / "bundles",
                )
            self.assertIn("verification was not supplied", str(raised.exception))

            calibration_calls = []
            failed = verification_report(current, display_match_rate=0.99)
            result = refresh_weekly_engine(
                current,
                formula_path=formula_path,
                bundle_directory=root / "bundles",
                verify_reuse=lambda *_: failed,
                calibrate=lambda *args: calibration_calls.append(args) or refresh_formula(),
            )
            self.assertEqual(len(calibration_calls), 1)
            self.assertEqual(result.formula_decision.action, FormulaAction.RECALIBRATE)
            self.assertEqual(result.reuse_verification, failed)
            self.assertIn("displayed", result.formula_decision.reasons[0])
            self.assertIsNone(
                result.bundle.methodology_attestation.reuse_verification
            )

    def test_rejects_stale_or_insufficient_verification_reports(self):
        cases = (
            (
                "too few holdouts",
                {"ordinary_power_holdout_ids": tuple(f"holdout-{i}" for i in range(99))},
                "at least 100",
            ),
            ("raw score", {"max_absolute_score_error": 1.000001e-6}, "score"),
            ("raw delta", {"max_absolute_delta_error": 1.000001e-6}, "delta"),
            ("display", {"display_match_rate": 0.999}, "displayed"),
            ("formula", {"formula_id": "stale"}, "different formula"),
            (
                "methodology",
                {"methodology_fingerprint_id": "stale"},
                "different analyzer methodology",
            ),
            ("snapshot", {"weekly_snapshot_id": "stale"}, "different snapshot"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula_path = root / "formula.json"
            save_strength_formula(refresh_formula(), formula_path)
            current = evidence()
            for label, changes, expected in cases:
                with self.subTest(label=label):
                    failed = verification_report(current, **changes)
                    with self.assertRaises(CalibrationRequired) as raised:
                        refresh_weekly_engine(
                            current,
                            formula_path=formula_path,
                            bundle_directory=root / "bundles",
                            verify_reuse=lambda *_, value=failed: value,
                        )
                    self.assertIn(expected, str(raised.exception))

    def test_rejects_malformed_verifier_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula_path = root / "formula.json"
            save_strength_formula(refresh_formula(), formula_path)
            with self.assertRaisesRegex(ValueError, "FormulaVerificationReport"):
                refresh_weekly_engine(
                    evidence(),
                    formula_path=formula_path,
                    bundle_directory=root / "bundles",
                    verify_reuse=lambda *_: None,
                )

    def test_missing_formula_calibrates_once_and_persists_exact_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def calibrate(refresh_evidence, method):
                calls.append((refresh_evidence, method))
                return refresh_formula()

            result = refresh_weekly_engine(
                evidence(),
                formula_path=root / "formula.json",
                bundle_directory=root / "bundles",
                calibrate=calibrate,
                verify_reuse=lambda *_: (_ for _ in ()).throw(
                    AssertionError("a missing formula cannot be verified for reuse")
                ),
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.formula_decision.action, FormulaAction.RECALIBRATE)
            self.assertEqual(
                load_strength_formula(root / "formula.json"), refresh_formula()
            )
            saved_bundles = tuple((root / "bundles").glob("*.json"))
            self.assertEqual(len(saved_bundles), 1)
            self.assertTrue(saved_bundles[0].samefile(result.bundle_path))
            self.assertEqual(
                result.bundle.methodology_attestation.formula_id,
                result.formula.formula_id,
            )

    def test_surrogate_is_default_off_nonreusable_and_cannot_overwrite_exact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula_path = root / "formula.json"
            exact = refresh_formula()
            save_strength_formula(exact, formula_path)

            with self.assertRaisesRegex(ValueError, "explicit opt-in"):
                refresh_weekly_engine(
                    evidence(),
                    formula_path=formula_path,
                    bundle_directory=root / "rejected",
                    force_recalibration=True,
                    calibrate=lambda *_: surrogate_formula(),
                )
            self.assertEqual(load_strength_formula(formula_path), exact)

            progress = []
            result = refresh_weekly_engine(
                evidence(),
                formula_path=formula_path,
                bundle_directory=root / "accepted",
                force_recalibration=True,
                calibrate=lambda *_: surrogate_formula(),
                allow_surrogate_power=True,
                progress=progress.append,
            )

            self.assertEqual(result.bundle.methodology_mode, "surrogate")
            self.assertIsNone(result.bundle.methodology_attestation)
            self.assertIsNotNone(result.bundle.surrogate_disclosure)
            self.assertNotEqual(result.formula_path, formula_path.resolve())
            self.assertTrue(result.formula_path.is_file())
            self.assertEqual(load_strength_formula(formula_path), exact)
            self.assertIn("SURROGATE", progress[-1].message)

    def test_forced_recalibration_skips_reuse_verification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            formula_path = root / "formula.json"
            save_strength_formula(refresh_formula(), formula_path)
            calls = []
            result = refresh_weekly_engine(
                evidence(),
                formula_path=formula_path,
                bundle_directory=root / "bundles",
                force_recalibration=True,
                calibrate=lambda *args: calls.append(args) or refresh_formula(),
                verify_reuse=lambda *_: (_ for _ in ()).throw(
                    AssertionError("forced recalibration must skip reuse verification")
                ),
            )
            self.assertEqual(len(calls), 1)
            self.assertIsNone(result.reuse_verification)
            self.assertEqual(result.formula_decision.action, FormulaAction.RECALIBRATE)

    def test_reports_calibration_requirement_and_honors_cancellation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(CalibrationRequired):
                refresh_weekly_engine(
                    evidence(),
                    formula_path=root / "missing.json",
                    bundle_directory=root / "bundles",
                )
            with self.assertRaises(RefreshCancelled):
                refresh_weekly_engine(
                    evidence(),
                    formula_path=root / "missing.json",
                    bundle_directory=root / "bundles",
                    calibrate=lambda *_: refresh_formula(),
                    cancelled=lambda: True,
                )
            self.assertFalse((root / "bundles").exists())


if __name__ == "__main__":
    unittest.main()
