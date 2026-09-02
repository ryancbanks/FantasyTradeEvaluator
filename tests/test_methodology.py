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

    def test_rejects_cross_provider_power_leakage_but_ensemble_uses_all_three(self):
        for feature in (
            "projection_espn_remaining_points",
            "projection_yahoo_remaining_points",
            "projection_ensemble_remaining_points",
            "projection_sleeper_remaining_points",
        ):
            with self.subTest(feature=feature):
                with self.assertRaisesRegex(ValueError, "FantasyPros projection features"):
                    PowerMethodology((feature,), ("presence",))
        ensemble = default_projection_ensemble()
        self.assertEqual(
            {row.provider for row in ensemble.provider_weights},
            {"fantasypros", "espn", "yahoo"},
        )
        self.assertEqual(ensemble.minimum_observed_sources, 2)


if __name__ == "__main__":
    unittest.main()
