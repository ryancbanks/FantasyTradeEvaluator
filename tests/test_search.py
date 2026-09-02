from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import math
import unittest

from trade_snapshot.search import PreparedTradePair, TradeStrengthPrefilter
from trade_snapshot.strength import (
    CalibrationMetadata,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
    StrengthModel,
)
from trade_snapshot.trade_space import (
    TeamRoster,
    TradeCandidate,
    TradeConstraints,
    TradeSpace,
)


class CountingStrengthModel(StrengthModel):
    calls: list[tuple[str, ...]] = []

    def score_roster(self, roster_player_ids):
        player_ids = tuple(roster_player_ids)
        self.calls.append(player_ids)
        return super().score_roster(player_ids)


def model():
    CountingStrengthModel.calls = []
    return CountingStrengthModel(
        role_definitions=(
            RoleDefinition("QB", RoleKind.STARTER, "QB", frozenset({"QB"})),
        ),
        players=(
            PlayerStrength("a", 10, frozenset({"UNSCORED"}), {}),
            PlayerStrength("b", 20, frozenset({"UNSCORED"}), {}),
            PlayerStrength("x", 10.04, frozenset({"UNSCORED"}), {}),
            PlayerStrength("y", 25, frozenset({"UNSCORED"}), {}),
        ),
        normalization_denominator=100,
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="profile-1",
        calibration=CalibrationMetadata(
            analyzer_bundle_url=(
                "https://cdn.fantasypros.com/assets/trade-analyzer.js"
            ),
            analyzer_bundle_sha256="1" * 64,
            response_schema_sha256="2" * 64,
            captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
    )


def roster(team_id, player_ids, *, current_size=None, cap=None):
    player_ids = tuple(player_ids)
    size = len(player_ids) if current_size is None else current_size
    return TeamRoster(team_id, player_ids, size, size if cap is None else cap)


def pair_and_space():
    primary = roster("primary", ("a", "b"))
    counterparty = roster("counterparty", ("x", "y"))
    pair = PreparedTradePair(model(), primary, counterparty)
    space = TradeSpace(primary, counterparty, TradeConstraints())
    return pair, space


class PreparedTradePairTests(unittest.TestCase):
    def test_caches_each_full_roster_once_and_scores_only_post_trade_rosters(self):
        pair, _ = pair_and_space()

        self.assertEqual(
            CountingStrengthModel.calls,
            [("a", "b"), ("x", "y")],
        )

        result = pair.evaluate(
            TradeCandidate(("a",), ("x",)),
            candidate_index=7,
        )

        self.assertEqual(len(CountingStrengthModel.calls), 4)
        self.assertEqual(CountingStrengthModel.calls[2:], [("b", "x"), ("y", "a")])
        self.assertEqual(result.candidate_index, 7)
        self.assertAlmostEqual(result.primary_raw_delta, 0.04)
        self.assertAlmostEqual(result.counterparty_raw_delta, -0.04)
        self.assertEqual(result.primary_display_delta, 0.0)
        self.assertEqual(result.counterparty_display_delta, 0.0)

    def test_is_immutable_and_requires_distinct_complete_nonoverlapping_rosters(self):
        pair, _ = pair_and_space()
        with self.assertRaises(FrozenInstanceError):
            pair.primary = roster("changed", ("a",))

        good_model = model()
        with self.assertRaisesRegex(ValueError, "different team_id"):
            PreparedTradePair(
                good_model,
                roster("same", ("a",)),
                roster("same", ("x",)),
            )
        with self.assertRaisesRegex(ValueError, "both prepared teams"):
            PreparedTradePair(
                good_model,
                roster("primary", ("a",)),
                roster("counterparty", ("a",)),
            )
        with self.assertRaisesRegex(ValueError, "full current roster"):
            PreparedTradePair(
                good_model,
                roster("primary", ("a",), current_size=2, cap=2),
                roster("counterparty", ("x",)),
            )

    def test_rejects_empty_duplicate_unowned_packages_and_bad_indices(self):
        pair, _ = pair_and_space()
        cases = (
            (TradeCandidate((), ("x",)), 0, "at least one player"),
            (TradeCandidate(("a", "a"), ("x",)), 0, "duplicate player_id"),
            (TradeCandidate(("x",), ("y",)), 0, "not on the primary"),
            (TradeCandidate(("a",), ("a",)), 0, "not on the counterparty"),
            (TradeCandidate(("a",), ("x",)), -1, "non-negative integer"),
            (TradeCandidate(("a",), ("x",)), True, "non-negative integer"),
        )
        for candidate, index, message in cases:
            with self.subTest(candidate=candidate, index=index):
                with self.assertRaisesRegex(ValueError, message):
                    pair.evaluate(candidate, candidate_index=index)


class TradeStrengthPrefilterTests(unittest.TestCase):
    def test_is_lazy_preserves_indices_and_tracks_progress_without_materializing(self):
        pair, space = pair_and_space()

        results = pair.prefilter(space)

        self.assertIsInstance(results, TradeStrengthPrefilter)
        self.assertIs(iter(results), results)
        self.assertEqual(results.minimum_displayed_power_delta, -5.0)
        self.assertEqual(results.examined_count, 0)
        self.assertEqual(results.qualified_count, 0)
        self.assertEqual(len(CountingStrengthModel.calls), 2)

        first = next(results)
        self.assertEqual(first.candidate_index, 0)
        self.assertEqual(results.examined_count, 1)
        self.assertEqual(results.qualified_count, 1)

        remainder = list(results)
        self.assertEqual([result.candidate_index for result in remainder], [3])
        self.assertEqual(results.examined_count, space.candidate_count)
        self.assertEqual(results.qualified_count, 2)
        self.assertEqual(len(CountingStrengthModel.calls), 2 + 2 * space.candidate_count)

    def test_custom_threshold_applies_to_both_teams_inclusively(self):
        pair, space = pair_and_space()

        nonnegative = list(
            pair.prefilter(space, minimum_displayed_power_delta=0)
        )

        self.assertEqual([result.candidate_index for result in nonnegative], [0])

    def test_rejects_mismatched_spaces_and_nonfinite_thresholds(self):
        pair, _ = pair_and_space()
        other_primary = roster("primary", ("b", "a"))
        mismatch = TradeSpace(
            other_primary,
            pair.counterparty,
            TradeConstraints(),
        )
        with self.assertRaisesRegex(ValueError, "must match"):
            pair.prefilter(mismatch)
        with self.assertRaisesRegex(ValueError, "must match"):
            TradeStrengthPrefilter(pair, mismatch)

        matching = TradeSpace(pair.primary, pair.counterparty, TradeConstraints())
        for value in (math.nan, math.inf, -math.inf, True, "-5"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite number"):
                    pair.prefilter(
                        matching,
                        minimum_displayed_power_delta=value,
                    )


if __name__ == "__main__":
    unittest.main()
