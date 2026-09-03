import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot.draft_config import (
    DraftLeagueConfig,
    DraftStrategy,
    config_from_engine_bundle,
    default_slot_eligibility,
    score_raw_stats,
    scoring_weights_from_profile,
)
from trade_snapshot.scoring import ScoringProfile
from trade_snapshot.positions import CANONICAL_PLAYER_POSITIONS


class DraftConfigTests(unittest.TestCase):
    def test_standard_config_round_trip_and_strategy_thresholds(self):
        config = DraftLeagueConfig.standard_ppr(team_count=10)
        restored = DraftLeagueConfig.from_record(config.to_record())
        self.assertEqual(restored.config_id, config.config_id)
        self.assertEqual(restored.roster_size, 16)
        self.assertEqual(restored.position_limits["WR"], 6)
        self.assertFalse(DraftStrategy.STREAMING_QB.allows("QB", 13, 16))
        self.assertTrue(DraftStrategy.STREAMING_QB.allows("QB", 14, 16))
        self.assertFalse(DraftStrategy.STREAMING_TE.allows("TE", 13, 16))
        self.assertTrue(DraftStrategy.STREAMING_DST.allows("DST", 16, 16))
        self.assertFalse(DraftStrategy.LATE_ROUND_QB.allows("QB", 9, 16))
        self.assertTrue(DraftStrategy.LATE_ROUND_QB.allows("QB", 10, 16))

    def test_strategy_counts_must_equal_league_size(self):
        with self.assertRaisesRegex(ValueError, "add up"):
            DraftLeagueConfig(
                "bad", 4, ("QB",), 1, default_slot_eligibility(("QB",)), {},
                {"passing_tds": 4}, (1,), 2, (2,),
                {DraftStrategy.NONE: 3},
            )

    def test_position_limits_must_allow_a_complete_starting_lineup(self):
        with self.assertRaisesRegex(ValueError, "position_limits"):
            DraftLeagueConfig(
                "bad limits", 2, ("RB", "RB"), 1,
                default_slot_eligibility(("RB", "RB")), {"RB": 1},
                {"rushing_yards": 0.1}, (1,), 2, (2,),
                {DraftStrategy.NONE: 2},
            )

    def test_position_limits_must_allow_a_complete_roster_when_all_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "complete configured roster"):
            DraftLeagueConfig(
                "bad roster limits", 2, ("QB",), 10,
                default_slot_eligibility(("QB",)),
                {position: 1 for position in CANONICAL_PLAYER_POSITIONS},
                {"passing_yards": 0.04}, (1,), 2, (2,),
                {DraftStrategy.NONE: 2},
            )

    def test_starting_slots_are_bounded_before_exponential_lineup_work(self):
        slots = ("FLEX",) * 17
        with self.assertRaisesRegex(ValueError, "slot count"):
            DraftLeagueConfig(
                "too many starters", 2, slots, 0,
                default_slot_eligibility(slots), {}, {"yards": 0.1},
                (1,), 2, (2,), {DraftStrategy.NONE: 2},
            )

    def test_raw_stats_support_custom_scoring_and_ignore_unweighted_fields(self):
        self.assertEqual(
            score_raw_stats(
                {"passing_yards": 250, "passing_tds": 2, "unknown": 999},
                {"passing_yards": 0.04, "passing_tds": 6},
            ),
            22,
        )

    def test_manual_scoring_requires_portable_names_and_a_nonzero_rule(self):
        base = DraftLeagueConfig.standard_ppr(team_count=2)
        for scoring, message in (({"Passing Yards": 0.04}, "lowercase"), ({"yards": 0}, "non-zero")):
            with self.subTest(scoring=scoring):
                with self.assertRaisesRegex(ValueError, message):
                    DraftLeagueConfig(
                        "bad scoring", 2, base.starting_slots, base.bench_slots,
                        base.slot_eligibility, base.position_limits, scoring,
                        base.regular_season_weeks, base.playoff_team_count,
                        base.playoff_weeks, base.strategy_counts,
                    )

    def test_legacy_flat_scoring_profile_is_not_guessed(self):
        bundle = engine_bundle()
        with self.assertRaisesRegex(ValueError, "linear scoring"):
            config_from_engine_bundle(bundle)

    def test_versioned_normalized_scoring_envelope_is_supported(self):
        profile = ScoringProfile("yahoo", {
            "normalized_linear_stat_weights_version": 1,
            "normalized_linear_stat_weights": {"passing_yards": 0.04, "receptions": 1},
            "unrelated_metadata": 99,
        })
        self.assertEqual(
            scoring_weights_from_profile(profile),
            {"passing_yards": 0.04, "receptions": 1.0},
        )

    def test_arbitrary_platform_settings_are_never_inferred_as_weights(self):
        self.assertEqual(
            scoring_weights_from_profile(ScoringProfile("yahoo", {
                "passing_yards_per_point": 25, "minimum_attempts": 10,
            })),
            {},
        )

    def test_espn_scoring_items_do_not_flatten_metadata_into_stat_weights(self):
        profile = ScoringProfile("espn", {
            "adapter_version": "fixture",
            "scoring_settings": {
                "homeTeamBonus": 7,
                "scoringItems": [
                    {"statId": 53, "points": 1},
                    {"statId": 4, "points": 4},
                ],
            },
        })
        self.assertEqual(
            scoring_weights_from_profile(profile),
            {"espn_stat_4": 4.0, "espn_stat_53": 1.0},
        )


if __name__ == "__main__":
    unittest.main()
