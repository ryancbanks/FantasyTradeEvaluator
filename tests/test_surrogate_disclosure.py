import copy
from dataclasses import replace
import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot._app_support import bundle_summary
from trade_snapshot.engine_bundle import EngineBundle
from trade_snapshot.methodology_reuse import FormulaAction, FormulaReuseDecision
from trade_snapshot.strength import CalibrationStatus
from trade_snapshot.strength import StrengthModel
from trade_snapshot.surrogate_disclosure import (
    SURROGATE_NOTICE,
    SurrogateDisclosure,
)


def surrogate_bundle():
    exact = engine_bundle()
    attestation = exact.methodology_attestation
    calibration = replace(
        exact.strength_model.calibration,
        status=CalibrationStatus.SURROGATE,
        max_absolute_score_error=0.25,
        display_match_rate=0.91,
    )
    model = StrengthModel(
        exact.strength_model.role_definitions,
        exact.strength_model.players.values(),
        exact.strength_model.normalization_denominator,
        snapshot_id=exact.strength_model.snapshot_id,
        season=exact.strength_model.season,
        scoring_profile_id=exact.strength_model.scoring_profile_id,
        calibration=calibration,
    )
    decision = FormulaReuseDecision(
        FormulaAction.RECALIBRATE,
        ("blind exactness checks failed",),
        attestation.methodology_fingerprint.fingerprint_id,
    )
    disclosure = SurrogateDisclosure(
        weekly_snapshot_id=exact.state.snapshot_id,
        strength_model_id=model.model_id,
        formula_id=attestation.formula_id,
        formula_source_fit_id="surrogate-fit-1",
        formula_trained_snapshot_id=exact.state.snapshot_id,
        methodology_fingerprint=attestation.methodology_fingerprint,
        formula_decision=decision,
        calibration_diagnostics=calibration,
        held_out_trade_ids=attestation.calibration_holdout_ids,
        observed_balanced_package_sizes=(1, 2, 3, 4),
    )
    return replace(
        exact,
        strength_model=model,
        methodology_attestation=None,
        surrogate_disclosure=disclosure,
    )


class SurrogateDisclosureTests(unittest.TestCase):
    def test_is_distinct_from_attestation_and_never_labels_power_exact(self):
        bundle = surrogate_bundle()

        self.assertEqual(bundle.methodology_mode, "surrogate")
        self.assertIsNone(bundle.methodology_attestation)
        self.assertIs(bundle.methodology_evidence, bundle.surrogate_disclosure)
        for outgoing, incoming, adjusted, expected in (
            (1, 1, False, "surrogate"),
            (4, 4, False, "surrogate"),
            (1, 2, True, "surrogate_extrapolated"),
            (9, 9, False, "surrogate_extrapolated"),
        ):
            self.assertEqual(
                bundle.methodology_evidence.power_result_status(
                    outgoing_count=outgoing,
                    incoming_count=incoming,
                    has_roster_adjustment=adjusted,
                ),
                expected,
            )

        summary = bundle_summary(bundle)
        self.assertEqual(summary["power_engine_mode"], "surrogate")
        self.assertEqual(summary["power_engine_notice"], SURROGATE_NOTICE)
        self.assertIsNone(summary["methodology"]["attestation_id"])
        self.assertEqual(
            summary["methodology"]["surrogate_disclosure_id"],
            bundle.surrogate_disclosure.disclosure_id,
        )
        self.assertEqual(
            summary["methodology"]["validated_balanced_package_sizes"], []
        )

    def test_strict_round_trip_and_tamper_detection(self):
        bundle = surrogate_bundle()
        record = bundle.to_record()

        self.assertEqual(record["schema_version"], 6)
        self.assertIsNone(record["methodology_attestation"])
        self.assertEqual(EngineBundle.from_record(record), bundle)

        tampered = copy.deepcopy(record)
        tampered["surrogate_disclosure"]["formula_id"] = "changed"
        with self.assertRaisesRegex(ValueError, "disclosure_id"):
            EngineBundle.from_record(tampered)

    def test_bundle_requires_exactly_one_methodology_record(self):
        exact = engine_bundle()
        surrogate = surrogate_bundle()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            replace(exact, methodology_attestation=None)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            replace(exact, surrogate_disclosure=surrogate.surrogate_disclosure)


if __name__ == "__main__":
    unittest.main()
