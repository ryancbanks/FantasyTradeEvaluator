import unittest

from trade_snapshot.operation_timing import JS_SAFE_INTEGER, OperationTiming


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class OperationTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.timing = OperationTiming(clock=self.clock)

    def test_queued_and_indeterminate_snapshots_have_stable_versioned_shape(self):
        queued = self.timing.snapshot()
        self.assertEqual(queued["schema_version"], 1)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["activity"], "idle")
        self.assertIsNone(queued["phase"])
        self.assertEqual(queued["elapsed_seconds"], 0)
        self.assertFalse(queued["cancel_requested"])
        self.assertEqual(
            queued["progress"],
            {
                "determinate": False,
                "fraction": None,
                "completed_units": None,
                "completed_units_text": None,
                "total_units": None,
                "total_units_text": None,
            },
        )
        self.assertIsNone(queued["eta"])

        started = self.timing.start("preparing")
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["activity"], "active")
        self.assertEqual(started["phase"], "preparing")
        self.assertFalse(started["progress"]["determinate"])

    def test_pause_excludes_user_wait_and_terminal_elapsed_time_is_frozen(self):
        self.timing.start("collecting")
        self.clock.advance(2.25)
        paused = self.timing.pause()
        self.assertEqual(paused["activity"], "paused")
        self.assertEqual(paused["elapsed_seconds"], 2.25)

        self.clock.advance(100)
        self.assertEqual(self.timing.snapshot()["elapsed_seconds"], 2.25)
        self.timing.resume()
        self.clock.advance(3.5)
        complete = self.timing.finish()
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["activity"], "terminal")
        self.assertEqual(complete["elapsed_seconds"], 5.75)
        self.assertIsNone(complete["eta"])

        self.clock.advance(50)
        self.assertEqual(self.timing.snapshot()["elapsed_seconds"], 5.75)

    def test_large_exact_units_use_decimal_text_without_unsafe_json_numbers(self):
        completed = JS_SAFE_INTEGER + 1
        total = completed + 99
        row = self.timing.start(
            "enumerating",
            completed_units=completed,
            total_units=total,
        )
        self.assertTrue(row["progress"]["determinate"])
        self.assertIsNone(row["progress"]["completed_units"])
        self.assertEqual(row["progress"]["completed_units_text"], str(completed))
        self.assertIsNone(row["progress"]["total_units"])
        self.assertEqual(row["progress"]["total_units_text"], str(total))
        self.assertAlmostEqual(row["progress"]["fraction"], completed / total)

        # Arbitrarily large exact counters remain reportable even when a float
        # cannot represent their interval throughput.
        enormous = 10**1_000
        timing = OperationTiming(clock=self.clock, minimum_sample_seconds=0)
        timing.start("huge", completed_units=0, total_units=enormous * 2)
        self.clock.advance(1)
        huge_row = timing.observe(enormous, enormous * 2)
        self.assertEqual(huge_row["progress"]["completed_units_text"], str(enormous))
        self.assertIsNone(huge_row["eta"])

    def test_eta_requires_enough_observed_active_time_and_rate_samples(self):
        timing = OperationTiming(
            clock=self.clock,
            minimum_rate_samples=3,
            minimum_sample_seconds=3,
        )
        timing.start("searching", completed_units=0, total_units=100)
        for expected in (10, 20):
            self.clock.advance(1)
            row = timing.observe(expected, 100)
            self.assertIsNone(row["eta"])

        self.clock.advance(1)
        row = timing.observe(30, 100)
        self.assertEqual(
            row["eta"],
            {
                "low_seconds": 7,
                "likely_seconds": 7,
                "high_seconds": 7,
                "confidence": "low",
                "basis": "observed_phase_throughput",
                "sample_count": 3,
            },
        )

    def test_robust_rolling_ewma_does_not_let_one_burst_define_eta(self):
        timing = OperationTiming(
            clock=self.clock,
            minimum_rate_samples=3,
            minimum_sample_seconds=0,
        )
        timing.start("searching", completed_units=0, total_units=1_140)
        completed = 0
        for increment in (10, 10, 1_000, 10, 10):
            completed += increment
            self.clock.advance(1)
            row = timing.observe(completed, 1_140)
        eta = row["eta"]
        self.assertIsNotNone(eta)
        self.assertEqual(eta["likely_seconds"], 10)
        self.assertEqual(eta["low_seconds"], 10)
        self.assertEqual(eta["high_seconds"], 10)
        self.assertEqual(eta["confidence"], "medium")

    def test_pause_does_not_pollute_rate_and_phase_change_resets_samples(self):
        timing = OperationTiming(
            clock=self.clock,
            minimum_rate_samples=2,
            minimum_sample_seconds=2,
        )
        timing.start("first", completed_units=0, total_units=100)
        self.clock.advance(1)
        timing.observe(10, 100)
        timing.pause()
        self.clock.advance(1_000)
        self.assertIsNone(timing.snapshot()["eta"])
        timing.resume()
        self.clock.advance(1)
        row = timing.observe(20, 100)
        self.assertEqual(row["eta"]["likely_seconds"], 8)

        next_phase = timing.begin_phase(
            "second",
            completed_units=0,
            total_units=50,
        )
        self.assertEqual(next_phase["phase"], "second")
        self.assertIsNone(next_phase["eta"])
        self.clock.advance(1)
        self.assertIsNone(timing.observe(10, 50)["eta"])

    def test_cancellation_request_and_first_terminal_outcome_are_preserved(self):
        self.timing.start("searching")
        self.clock.advance(1)
        requested = self.timing.request_cancel()
        self.assertEqual(requested["status"], "running")
        self.assertTrue(requested["cancel_requested"])

        cancelled = self.timing.cancel()
        self.assertEqual(cancelled["status"], "cancelled")
        self.clock.advance(5)
        self.assertEqual(self.timing.finish()["status"], "cancelled")
        self.assertEqual(self.timing.snapshot()["elapsed_seconds"], 1)

        failed = OperationTiming(clock=FakeClock())
        self.assertEqual(failed.fail()["status"], "failed")
        self.assertEqual(failed.finish()["status"], "failed")

        completed = OperationTiming(clock=FakeClock())
        completed.finish()
        unchanged = completed.cancel()
        self.assertEqual(unchanged["status"], "complete")
        self.assertFalse(unchanged["cancel_requested"])

    def test_validation_rejects_ambiguous_or_regressing_progress(self):
        with self.assertRaisesRegex(ValueError, "provided together"):
            self.timing.start("phase", completed_units=0)
        self.timing.start("phase", completed_units=1, total_units=10)
        with self.assertRaisesRegex(ValueError, "move backwards"):
            self.timing.observe(0, 10)
        with self.assertRaisesRegex(ValueError, "cannot change"):
            self.timing.observe(2, 11)
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            self.timing.observe(True, 10)
        with self.assertRaisesRegex(RuntimeError, "while paused"):
            self.timing.pause()
            self.timing.observe(2, 10)

    def test_backwards_or_non_finite_clocks_are_rejected(self):
        self.timing.start("phase")
        self.clock.advance(1)
        self.timing.snapshot()
        self.clock.value = 0
        with self.assertRaisesRegex(RuntimeError, "backwards"):
            self.timing.snapshot()

        with self.assertRaisesRegex(ValueError, "finite"):
            OperationTiming(clock=lambda: float("inf"))


if __name__ == "__main__":
    unittest.main()
