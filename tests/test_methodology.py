import unittest

from trade_snapshot.methodology import (
    DEFAULT_POWER_METHODOLOGY,
    PowerMethodology,
    default_projection_ensemble,
)


class MethodologyTests(unittest.TestCase):
    def test_default_power_features_are_fantasypros_only_and_fit_ready(self):
        policy = DEFAULT_POWER_METHODOLOGY
        self.assertFalse(
            any(
                name.startswith(("projection_espn_", "projection_yahoo_"))
                for name in (*policy.residual_feature_names, *policy.role_feature_names)
            )
        )
        config = policy.fit_config()
        self.assertEqual(config.residual_feature_names, policy.residual_feature_names)
        self.assertEqual(config.role_feature_names, policy.role_feature_names)

    def test_default_power_does_not_require_unpublished_projection_horizons(self):
        policy = DEFAULT_POWER_METHODOLOGY
        required = (*policy.residual_feature_names, *policy.role_feature_names)
        self.assertFalse(any(name.startswith("projection_") for name in required))
        self.assertTrue(all(name == "presence" or name.startswith("ecr_ros_") for name in required))

    def test_rejects_cross_provider_power_leakage_and_defaults_to_core_ensemble(self):
        for feature in (
            "projection_espn_full_ros_points",
            "projection_yahoo_full_ros_points",
            "projection_ensemble_full_ros_points",
            "projection_sleeper_full_ros_points",
        ):
            with self.subTest(feature=feature):
                with self.assertRaisesRegex(ValueError, "FantasyPros projection features"):
                    PowerMethodology((feature,), ("presence",))
        ensemble = default_projection_ensemble()
        self.assertEqual(
            tuple(row.provider for row in ensemble.provider_weights),
            ("fantasypros", "espn", "yahoo"),
        )
        self.assertEqual(ensemble.minimum_observed_sources, 2)

    def test_projection_ensemble_supports_independent_or_composite_not_both(self):
        ensemble = default_projection_ensemble(("fantasypros",))
        self.assertEqual(
            tuple(row.provider for row in ensemble.provider_weights),
            ("fantasypros",),
        )
        self.assertEqual(ensemble.minimum_observed_sources, 1)

        independent = default_projection_ensemble(("fantasysharks", "espn", "cbs"))
        self.assertEqual(
            tuple(row.provider for row in independent.provider_weights),
            ("espn", "cbs", "fantasysharks"),
        )
        with self.assertRaisesRegex(ValueError, "composite projections"):
            default_projection_ensemble(("fantasypros", "espn"))
        yahoo = default_projection_ensemble(("yahoo",))
        self.assertEqual(
            tuple(row.provider for row in yahoo.provider_weights), ("yahoo",)
        )
        with self.assertRaisesRegex(ValueError, "reference-only"):
            default_projection_ensemble(("ffa",))
        with self.assertRaisesRegex(ValueError, "lowercase names"):
            default_projection_ensemble(("fantasypros", ["espn"]))


if __name__ == "__main__":
    unittest.main()
