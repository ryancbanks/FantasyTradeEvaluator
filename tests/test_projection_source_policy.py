import unittest

from trade_snapshot.projection_source_policy import (
    ProjectionSourceSelection,
    select_projection_sources,
    validate_no_composite_double_count,
)


class ProjectionSourcePolicyTests(unittest.TestCase):
    def test_broad_consensus_counts_only_independent_publishers_once(self):
        selection = select_projection_sources(
            (
                "fantasypros",
                "fantasysharks",
                "espn",
                "yahoo",
                "cbs",
                "fftoday",
            ),
            broad_consensus=True,
            fantasypros_available=True,
        )

        self.assertEqual(
            selection.providers,
            ("espn", "yahoo", "cbs", "fftoday", "fantasysharks"),
        )
        self.assertEqual(selection.minimum_observed_sources, 2)
        self.assertEqual(selection.mode, "broad_consensus")
        ensemble = selection.ensemble_config()
        self.assertEqual(
            tuple((row.provider, row.weight) for row in ensemble.provider_weights),
            tuple((provider, 1.0) for provider in selection.providers),
        )

    def test_fantasypros_composite_and_espn_fallbacks_are_explicit(self):
        fantasypros = select_projection_sources(
            ("fantasypros", "espn", "yahoo"),
            broad_consensus=False,
            fantasypros_available=True,
        )
        independent = select_projection_sources(
            ("espn", "yahoo"),
            broad_consensus=False,
            fantasypros_available=False,
        )

        self.assertEqual(fantasypros.providers, ("fantasypros", "espn", "yahoo"))
        self.assertEqual(fantasypros.mode, "core_ensemble")
        self.assertEqual(independent.providers, ("espn", "yahoo"))
        self.assertEqual(independent.mode, "core_ensemble")

    def test_yahoo_is_selectable_in_broad_and_core_ensembles(self):
        broad = select_projection_sources(
            ("fantasypros", "espn", "cbs", "yahoo"),
            broad_consensus=True,
            fantasypros_available=True,
        )
        fallback = select_projection_sources(
            ("espn", "yahoo"),
            broad_consensus=False,
            fantasypros_available=False,
        )

        self.assertEqual(broad.providers, ("espn", "yahoo", "cbs"))
        self.assertEqual(fallback.providers, ("espn", "yahoo"))
        self.assertEqual(
            ProjectionSourceSelection(("yahoo",), 1, "single_source").providers,
            ("yahoo",),
        )
        with self.assertRaisesRegex(ValueError, "reference-only"):
            ProjectionSourceSelection(("ffa",), 1, "single_source")

    def test_rejects_double_counting_and_insufficient_broad_coverage(self):
        with self.assertRaisesRegex(ValueError, "cannot be averaged"):
            validate_no_composite_double_count(("fantasypros", "espn"))
        with self.assertRaisesRegex(ValueError, "at least two independent"):
            select_projection_sources(
                ("fantasypros", "espn"),
                broad_consensus=True,
                fantasypros_available=True,
            )
        for captured, fantasypros_available in (
            (("fantasypros", "espn"), True),
            (("espn",), False),
        ):
            with self.subTest(captured=captured), self.assertRaisesRegex(
                ValueError, "ESPN and Yahoo"
            ):
                select_projection_sources(
                    captured,
                    broad_consensus=False,
                    fantasypros_available=fantasypros_available,
                )
        with self.assertRaisesRegex(ValueError, "multiple composite"):
            validate_no_composite_double_count(("fantasypros", "ffa"))
        validate_no_composite_double_count(("fantasypros", "espn", "yahoo"))

    def test_rejects_mismatched_availability_duplicates_and_unknown_sources(self):
        for providers, fantasypros_available, message in (
            (("espn",), True, "availability"),
            (("fantasypros",), False, "availability"),
            (("espn", "espn"), False, "duplicate"),
            (("espn", "madeup"), False, "unsupported"),
        ):
            with self.subTest(providers=providers), self.assertRaisesRegex(
                ValueError, message
            ):
                select_projection_sources(
                    providers,
                    broad_consensus=False,
                    fantasypros_available=fantasypros_available,
                )


if __name__ == "__main__":
    unittest.main()
