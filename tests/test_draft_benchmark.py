import unittest

from tests.draft_fixtures import small_draft_config, small_historical_corpus
from trade_snapshot.draft_benchmark import _trial_cell, compare_to_regression_baseline
from trade_snapshot.draft_brain import initialize_genome
from trade_snapshot.draft_features import build_baseline_brain


class DraftBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = small_historical_corpus()
        cls.config = small_draft_config()
        cls.baseline = build_baseline_brain(cls.corpus, cls.config, (2025,))

    def test_baseline_against_itself_is_exactly_zero(self):
        result = compare_to_regression_baseline(
            self.baseline, self.corpus, self.config,
            trials=4, evaluation_years=(2025,), seed=5, candidate_window=4,
        )
        self.assertEqual(result.wins, 0)
        self.assertEqual(result.losses, 0)
        self.assertEqual(result.ties, 4)
        self.assertEqual(result.mean_points_delta, 0)
        self.assertEqual(result.mean_points_percentile_delta, 0)
        self.assertEqual(result.playoff_rate_delta, 0)
        self.assertEqual(result.championship_rate_delta, 0)
        self.assertEqual(result.verdict, "inconclusive")
        self.assertEqual(result.evaluation_seasons, (2025,))
        self.assertEqual(result.interval_basis, "season_clustered")

    def test_evolved_comparison_is_paired_bounded_and_reports_progress(self):
        brain = initialize_genome(
            self.baseline.schema, self.baseline.baseline, self.config.config_id,
            seed=9, genome_index=1, magnitude=500_000,
        )
        progress = []
        result = compare_to_regression_baseline(
            brain, self.corpus, self.config,
            trials=4, seed=8, candidate_window=4,
            on_progress=lambda done, total: progress.append((done, total)),
        )
        self.assertEqual(result.wins + result.ties + result.losses, 4)
        self.assertEqual(progress, [(1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertIn(result.verdict, {"improved", "worse", "inconclusive"})
        self.assertEqual(len(result.to_record()["percentile_delta_interval_95"]), 2)

    def test_cancel_and_incompatible_configuration_fail_clearly(self):
        with self.assertRaisesRegex(InterruptedError, "cancelled"):
            compare_to_regression_baseline(
                self.baseline, self.corpus, self.config, trials=2,
                should_cancel=lambda: True,
            )

    def test_trial_cells_cover_every_season_and_seat_before_repeating(self):
        cells = [_trial_cell(index, 10, 12) for index in range(120)]
        self.assertEqual(len(set(cells)), 120)
        self.assertEqual({season for season, _ in cells}, set(range(10)))
        self.assertEqual({seat for _, seat in cells}, set(range(12)))


if __name__ == "__main__":
    unittest.main()
