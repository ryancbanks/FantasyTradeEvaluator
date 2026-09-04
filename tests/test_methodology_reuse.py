from dataclasses import replace
from datetime import datetime, timezone
import unittest

from trade_snapshot.analyzer_contract import BundleFingerprint
from trade_snapshot.methodology import DEFAULT_POWER_METHODOLOGY
from trade_snapshot.methodology_reuse import (
    FormulaAction,
    MethodologyFingerprint,
    decide_formula_reuse,
)
from trade_snapshot.strength import CalibrationStatus, RoleDefinition, RoleKind
from trade_snapshot.strength_calibration import CalibrationMetadata
from trade_snapshot.strength_formula import StrengthFormula


URL = "https://cdn.fantasypros.com/assets/js/trade-analyzer.js"
SHA = "a" * 64
SCHEMA = "b" * 64


def role():
    return RoleDefinition("RB_START_1", RoleKind.STARTER, "RB", frozenset({"RB"}))


def fingerprint(*, sha=SHA):
    return MethodologyFingerprint(
        BundleFingerprint(URL, sha),
        SCHEMA,
        DEFAULT_POWER_METHODOLOGY,
        (role(),),
    )


def formula(*, status=CalibrationStatus.EXACT, scoring_profile_id="profile-1"):
    exact = status is CalibrationStatus.EXACT
    metadata = CalibrationMetadata(
        URL,
        SHA,
        SCHEMA,
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        status,
        100 if exact else 0,
        0 if exact else None,
        1 if exact else None,
    )
    return StrengthFormula(
        "fit-1",
        "snapshot-1",
        2026,
        scoring_profile_id,
        (role(),),
        {name: 1 for name in DEFAULT_POWER_METHODOLOGY.residual_feature_names},
        {
            role().role_id: {
                name: 1 for name in DEFAULT_POWER_METHODOLOGY.role_feature_names
            }
        },
        metadata,
        tuple(f"holdout-{index:03d}" for index in range(100)) if exact else (),
        (1, 2, 3, 4) if exact else (),
    )


class MethodologyReuseTests(unittest.TestCase):
    def test_marks_exact_formula_eligible_when_every_method_input_matches(self):
        decision = decide_formula_reuse(
            formula(), fingerprint(), season=2026, scoring_profile_id="profile-1"
        )
        self.assertEqual(decision.action, FormulaAction.REUSE)
        self.assertEqual(decision.reasons, ())

    def test_recalibrates_for_missing_unproven_or_changed_formula(self):
        missing = decide_formula_reuse(
            None, fingerprint(), season=2026, scoring_profile_id="profile-1"
        )
        self.assertEqual(missing.action, FormulaAction.RECALIBRATE)
        self.assertIn("no saved formula", missing.reasons)

        unproven = decide_formula_reuse(
            formula(status=CalibrationStatus.UNVALIDATED),
            fingerprint(),
            season=2026,
            scoring_profile_id="profile-1",
        )
        self.assertIn("not exact", unproven.reasons[0])

        changed = decide_formula_reuse(
            formula(), fingerprint(sha="c" * 64), season=2027, scoring_profile_id="other"
        )
        self.assertEqual(changed.action, FormulaAction.RECALIBRATE)
        self.assertIn("season changed", changed.reasons)
        self.assertIn("league scoring profile changed", changed.reasons)
        self.assertIn("FantasyPros analyzer bundle content changed", changed.reasons)

    def test_recalibrates_a_formula_using_the_superseded_partial_horizon_feature(self):
        current = formula()
        legacy = replace(
            current,
            residual_weights={"projection_fantasypros_remaining_points": 1},
            role_weights={
                role().role_id: {
                    "projection_fantasypros_remaining_points": 1,
                }
            },
        )

        decision = decide_formula_reuse(
            legacy,
            fingerprint(),
            season=2026,
            scoring_profile_id="profile-1",
        )

        self.assertEqual(decision.action, FormulaAction.RECALIBRATE)
        self.assertIn("FantasyPros power feature policy changed", decision.reasons)
        self.assertIn("FantasyPros role feature policy changed", decision.reasons)

    def test_fingerprint_is_content_addressed_and_order_independent(self):
        first = fingerprint()
        second = MethodologyFingerprint(
            first.analyzer_bundle,
            first.response_schema_sha256,
            first.power_methodology,
            tuple(reversed(first.role_definitions)),
        )
        self.assertEqual(first.fingerprint_id, second.fingerprint_id)

        relocated = MethodologyFingerprint(
            BundleFingerprint(
                "https://cdn.fantasypros.com/assets/js/renamed-trade-analyzer.js",
                first.analyzer_bundle.sha256,
            ),
            first.response_schema_sha256,
            first.power_methodology,
            first.role_definitions,
        )
        self.assertEqual(first.fingerprint_id, relocated.fingerprint_id)
        decision = decide_formula_reuse(
            formula(), relocated, season=2026, scoring_profile_id="profile-1"
        )
        self.assertEqual(decision.action, FormulaAction.REUSE)

    def test_fingerprint_and_decision_have_strict_portable_records(self):
        value = fingerprint()
        self.assertEqual(MethodologyFingerprint.from_record(value.to_record()), value)
        decision = decide_formula_reuse(
            formula(), value, season=2026, scoring_profile_id="profile-1"
        )
        self.assertEqual(type(decision).from_record(decision.to_record()), decision)


if __name__ == "__main__":
    unittest.main()
