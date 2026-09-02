import copy
from datetime import datetime, timezone
import unittest

from trade_snapshot.formula_verification import (
    FormulaVerificationReport,
    MAXIMUM_EXACT_VERIFICATION_ERROR,
    MINIMUM_WEEKLY_VERIFICATION_HOLDOUTS,
)


def report(**changes):
    values = {
        "formula_id": "formula-1",
        "methodology_fingerprint_id": "method-1",
        "weekly_snapshot_id": "week-1",
        "ordinary_power_holdout_ids": tuple(
            f"holdout-{index}"
            for index in range(MINIMUM_WEEKLY_VERIFICATION_HOLDOUTS)
        ),
        "balanced_package_sizes": (1, 2, 3, 4),
        "max_absolute_score_error": MAXIMUM_EXACT_VERIFICATION_ERROR,
        "max_absolute_delta_error": MAXIMUM_EXACT_VERIFICATION_ERROR,
        "display_match_rate": 1.0,
        "verified_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }
    values.update(changes)
    return FormulaVerificationReport(**values)


class FormulaVerificationReportTests(unittest.TestCase):
    def test_accepts_the_strict_boundary_for_the_current_context(self):
        value = report()
        self.assertEqual(
            value.rejection_reasons(
                formula_id="formula-1",
                methodology_fingerprint_id="method-1",
                weekly_snapshot_id="week-1",
            ),
            (),
        )

    def test_reports_every_failed_reuse_requirement(self):
        value = report(
            formula_id="old-formula",
            methodology_fingerprint_id="old-method",
            weekly_snapshot_id="old-week",
            ordinary_power_holdout_ids=("only-one",),
            max_absolute_score_error=1.1e-6,
            max_absolute_delta_error=1.2e-6,
            display_match_rate=0.99,
        )
        reasons = value.rejection_reasons(
            formula_id="formula-1",
            methodology_fingerprint_id="method-1",
            weekly_snapshot_id="week-1",
        )
        self.assertEqual(len(reasons), 7)

    def test_is_content_addressed_and_round_trips_strictly(self):
        value = report()
        self.assertEqual(FormulaVerificationReport.from_record(value.to_record()), value)
        tampered = copy.deepcopy(value.to_record())
        tampered["ordinary_power_holdout_ids"][0] = "changed"
        with self.assertRaisesRegex(ValueError, "verification_id"):
            FormulaVerificationReport.from_record(tampered)

    def test_reuse_requires_multi_package_scope(self):
        reasons = report(balanced_package_sizes=(1, 2)).rejection_reasons(
            formula_id="formula-1",
            methodology_fingerprint_id="method-1",
            weekly_snapshot_id="week-1",
        )
        self.assertIn("package sizes 3, 4", reasons[0])

    def test_rejects_duplicate_holdouts_and_invalid_metrics(self):
        with self.assertRaisesRegex(ValueError, "must be distinct"):
            report(ordinary_power_holdout_ids=("duplicate", "duplicate"))
        for value in (None, "one-holdout"):
            with self.subTest(holdouts=value):
                with self.assertRaisesRegex(ValueError, "collection"):
                    report(ordinary_power_holdout_ids=value)
        for name, value in (
            ("max_absolute_score_error", float("nan")),
            ("max_absolute_delta_error", -1),
            ("display_match_rate", 1.01),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    report(**{name: value})


if __name__ == "__main__":
    unittest.main()
