import unittest
from dataclasses import replace
from datetime import datetime, timezone
from itertools import combinations, product

from trade_snapshot import CURRENT_BUNDLE_FINGERPRINT
from trade_snapshot.calibration_fit import (
    CalibrationCorpus,
    CalibrationFitConfig,
    CalibrationTradeObservation,
    PlayerFeatureVector,
    RosterPowerSample,
    fit_strength_surrogate,
)
from trade_snapshot.strength import CalibrationStatus, RoleDefinition, RoleKind
from trade_snapshot.trade_space import TeamRoster


SCHEMA_SHA = "a" * 64


def feature(player_id, value, *, role_value=None):
    return PlayerFeatureVector(
        player_id,
        frozenset({"RB"}),
        {"role": value if role_value is None else role_value, "value": value},
    )


def sample(sample_id, team_id, roster, score):
    return RosterPowerSample(sample_id, team_id, roster, score)


def heldout(trade_id="held-1"):
    return CalibrationTradeObservation(
        trade_id=trade_id,
        team1_id="a",
        team2_id="b",
        team1_before_player_ids=("p1", "p2", "p5"),
        team1_after_player_ids=("p2", "p3", "p5"),
        team2_before_player_ids=("p3", "p4", "p6"),
        team2_after_player_ids=("p1", "p4", "p6"),
        team1_raw_before=100.0,
        team1_raw_after=100.0 * 14.0 / 17.0,
        team2_raw_before=100.0 * 14.0 / 17.0,
        team2_raw_after=100.0,
    )


def corpus(*, include_holdout=True, reverse=False, extra_samples=()):
    role = RoleDefinition("RB_START_1", RoleKind.STARTER, "RB", frozenset({"RB"}))
    rows = [
        feature("p1", 9), feature("p2", 3), feature("p3", 7),
        feature("p4", 5), feature("p5", 4), feature("p6", 2),
    ]
    samples = [
        sample("base-a", "a", ("p1", "p2", "p5"), 100.0),
        sample("base-b", "b", ("p3", "p4", "p6"), 100.0 * 14.0 / 17.0),
        sample("train-a", "a", ("p1", "p4", "p5"), 100.0 * 18.0 / 17.0),
        sample("train-b", "b", ("p2", "p3", "p6"), 100.0 * 13.0 / 17.0),
        *extra_samples,
    ]
    rosters = [
        TeamRoster("b", ("p6", "p4", "p3"), 3, 3),
        TeamRoster("a", ("p5", "p2", "p1"), 3, 3),
    ]
    if reverse:
        rows.reverse()
        samples.reverse()
        rosters.reverse()
    return CalibrationCorpus(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="scoring-1",
        role_definitions=(role,),
        player_features=rows,
        baseline_rosters=rosters,
        samples=samples,
        held_out_trades=(heldout(),) if include_holdout else (),
    )


def fit(value, *, minimum_exact_holdouts=100, ridge=0.0):
    return fit_strength_surrogate(
        value,
        CalibrationFitConfig(
            residual_feature_names=("value",),
            role_feature_names=("role",),
            ridge_penalty=ridge,
            convergence_tolerance=1e-10,
            minimum_exact_holdouts=minimum_exact_holdouts,
        ),
        bundle=CURRENT_BUNDLE_FINGERPRINT,
        response_schema_sha256=SCHEMA_SHA,
        captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def exact_corpus():
    role = RoleDefinition("RB_START_1", RoleKind.STARTER, "RB", frozenset({"RB"}))
    values = {
        f"p{index:02d}": 1.17 * index + 0.013 * (index % 3) + 1e-6 * index**2
        for index in range(1, 15)
    }
    left = tuple(f"p{index:02d}" for index in range(1, 8))
    right = tuple(f"p{index:02d}" for index in range(8, 15))

    def absolute(roster):
        return 0.5 * sum(values[player] for player in roster) + max(values[player] for player in roster)

    denominator = max(absolute(left), absolute(right))
    power = lambda roster: 100 * absolute(roster) / denominator
    trades = []
    packages = [((out,), (incoming,)) for out, incoming in product(left, right)]
    packages.extend(product(combinations(left, 2), combinations(right, 2)))
    for index, (outgoing, incoming) in enumerate(packages[:100]):
        after_left = tuple(sorted(set(left).difference(outgoing).union(incoming)))
        after_right = tuple(sorted(set(right).difference(incoming).union(outgoing)))
        trades.append(
            CalibrationTradeObservation(
                f"trade-{index:03d}", "a", "b", left, after_left, right, after_right,
                power(left), power(after_left), power(right), power(after_right),
            )
        )
    return CalibrationCorpus(
        snapshot_id="large-snapshot",
        season=2026,
        scoring_profile_id="scoring-1",
        role_definitions=(role,),
        player_features=tuple(feature(player, value) for player, value in values.items()),
        baseline_rosters=(TeamRoster("a", left, 7, 7), TeamRoster("b", right, 7, 7)),
        samples=(sample("base-a", "a", left, power(left)), sample("base-b", "b", right, power(right))),
        held_out_trades=tuple(trades),
    )


def feature_identical_trade_corpus(*, incoming_offset=0.0):
    role = RoleDefinition("RB_START_1", RoleKind.STARTER, "RB", frozenset({"RB"}))
    left = tuple(f"left-{index:03d}" for index in range(100)) + ("left-anchor",)
    right = tuple(f"right-{index:03d}" for index in range(100)) + ("right-anchor",)
    values = {
        **{f"left-{index:03d}": float(index + 1) for index in range(100)},
        **{
            f"right-{index:03d}": float(index + 1) + incoming_offset
            for index in range(100)
        },
        "left-anchor": 1000.0,
        "right-anchor": 900.0,
    }

    def absolute(roster):
        return 0.5 * sum(values[player] for player in roster) + max(values[player] for player in roster)

    denominator = max(absolute(left), absolute(right))
    power = lambda roster: 100 * absolute(roster) / denominator
    trades = []
    for index in range(100):
        outgoing, incoming = f"left-{index:03d}", f"right-{index:03d}"
        after_left = tuple(sorted(set(left).difference({outgoing}).union({incoming})))
        after_right = tuple(sorted(set(right).difference({incoming}).union({outgoing})))
        trades.append(
            CalibrationTradeObservation(
                f"identical-{index:03d}", "a", "b", left, after_left, right,
                after_right, power(left), power(after_left), power(right), power(after_right),
            )
        )
    return CalibrationCorpus(
        snapshot_id="feature-identical",
        season=2026,
        scoring_profile_id="scoring-1",
        role_definitions=(role,),
        player_features=tuple(feature(player, value) for player, value in values.items()),
        baseline_rosters=(
            TeamRoster("a", left, 101, 101),
            TeamRoster("b", right, 101, 101),
        ),
        samples=(sample("base-a", "a", left, power(left)), sample("base-b", "b", right, power(right))),
        held_out_trades=tuple(trades),
    )


class CalibrationCorpusTests(unittest.TestCase):
    def test_content_identity_is_independent_of_input_order(self):
        left, right = corpus(), corpus(reverse=True)
        self.assertEqual(left.corpus_id, right.corpus_id)
        self.assertEqual(tuple(left.baseline_rosters), ("a", "b"))
        self.assertEqual(left.roster_cap, 3)
        with self.assertRaises(TypeError):
            left.baseline_rosters["c"] = ("p1",)

    def test_atomic_trade_is_orientation_independent_and_conserves_players(self):
        first = heldout("one")
        reversed_trade = CalibrationTradeObservation(
            trade_id="two",
            team1_id=first.team2_id,
            team2_id=first.team1_id,
            team1_before_player_ids=first.team2_before_player_ids,
            team1_after_player_ids=first.team2_after_player_ids,
            team2_before_player_ids=first.team1_before_player_ids,
            team2_after_player_ids=first.team1_after_player_ids,
            team1_raw_before=first.team2_raw_before,
            team1_raw_after=first.team2_raw_after,
            team2_raw_before=first.team1_raw_before,
            team2_raw_after=first.team1_raw_after,
        )
        self.assertEqual(first.observation_id, reversed_trade.observation_id)
        with self.assertRaisesRegex(ValueError, "conserve"):
            CalibrationTradeObservation(
                "bad", "a", "b", ("p1",), ("p2",), ("p3",), ("p4",),
                100, 90, 80, 90,
            )

    def test_rejects_semantic_duplicates_and_train_holdout_leakage(self):
        with self.assertRaisesRegex(ValueError, "repeated semantic trade"):
            base = corpus(include_holdout=False)
            CalibrationCorpus(
                snapshot_id=base.snapshot_id,
                season=base.season,
                scoring_profile_id=base.scoring_profile_id,
                role_definitions=base.role_definitions,
                player_features=base.player_features,
                baseline_rosters=(
                    TeamRoster("a", ("p1", "p2", "p5"), 3, 3),
                    TeamRoster("b", ("p3", "p4", "p6"), 3, 3),
                ),
                samples=base.samples,
                held_out_trades=(heldout("one"), heldout("two")),
            )
        leaked = sample("leak", "a", ("p2", "p3", "p5"), 100 * 14 / 17)
        with self.assertRaisesRegex(ValueError, "leaks into training"):
            corpus(extra_samples=(leaked,))

        row = heldout("wrong-baseline")
        wrong_baseline = replace(
            row,
            team1_raw_before=row.team1_raw_before + 500,
            team1_raw_after=row.team1_raw_after + 500,
        )
        with self.assertRaisesRegex(ValueError, "raw-before score"):
            base = corpus(include_holdout=False)
            CalibrationCorpus(
                snapshot_id=base.snapshot_id,
                season=base.season,
                scoring_profile_id=base.scoring_profile_id,
                role_definitions=base.role_definitions,
                player_features=base.player_features,
                baseline_rosters=(
                    TeamRoster("a", ("p1", "p2", "p5"), 3, 3),
                    TeamRoster("b", ("p3", "p4", "p6"), 3, 3),
                ),
                samples=base.samples,
                held_out_trades=(wrong_baseline,),
            )

    def test_requires_full_typed_baselines_and_numeric_bounds(self):
        with self.assertRaisesRegex(ValueError, "complete current roster"):
            base = corpus(include_holdout=False)
            CalibrationCorpus(
                snapshot_id="s",
                season=2026,
                scoring_profile_id="c",
                role_definitions=base.role_definitions,
                player_features=base.player_features,
                baseline_rosters=(
                    TeamRoster("a", ("p1",), 3, 3),
                    TeamRoster("b", ("p3", "p4", "p6"), 3, 3),
                ),
                samples=base.samples,
            )
        with self.assertRaisesRegex(ValueError, "numeric range"):
            feature("huge", 1e200)


class FeatureCalibrationTests(unittest.TestCase):
    def test_fits_roles_and_validates_unseen_trade_deltas(self):
        result = fit(corpus())
        self.assertTrue(result.diagnostics.converged)
        self.assertTrue(result.diagnostics.identifiable)
        self.assertEqual(result.model.calibration.status, CalibrationStatus.SURROGATE)
        self.assertEqual(result.diagnostics.held_out_trade_count, 1)
        self.assertLess(result.diagnostics.training_max_absolute_error, 1e-7)
        self.assertLess(result.diagnostics.holdout_max_absolute_score_error, 1e-7)
        self.assertLess(result.diagnostics.holdout_max_delta_error, 1e-7)
        self.assertEqual(result.diagnostics.holdout_display_match_rate, 1.0)
        self.assertAlmostEqual(result.residual_weights["value"], 50 / 17, places=5)
        self.assertAlmostEqual(result.role_weights["RB_START_1"]["role"], 100 / 17, places=5)
        changed = replace(
            result,
            diagnostics=replace(
                result.diagnostics, held_out_distinct_perturbation_count=0
            ),
        )
        self.assertNotEqual(result.fit_id, changed.fit_id)

    def test_exact_requires_atomic_trade_count_and_all_error_gates(self):
        with self.assertRaisesRegex(ValueError, "cannot be below 100"):
            CalibrationFitConfig(("value",), ("role",), minimum_exact_holdouts=99)

        result = fit(exact_corpus())
        self.assertEqual(result.model.calibration.status, CalibrationStatus.EXACT)
        self.assertEqual(result.model.calibration.held_out_trade_count, 100)
        self.assertEqual(result.diagnostics.held_out_distinct_perturbation_count, 100)

    def test_roster_distinct_but_feature_identical_trades_cannot_be_exact(self):
        result = fit(feature_identical_trade_corpus())
        self.assertEqual(result.diagnostics.held_out_trade_count, 100)
        self.assertEqual(result.diagnostics.held_out_distinct_perturbation_count, 0)
        self.assertEqual(result.model.calibration.status, CalibrationStatus.SURROGATE)
        self.assertEqual(
            result.diagnostics.to_record()["held_out_distinct_perturbation_count"], 0
        )
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            replace(result.diagnostics, held_out_distinct_perturbation_count=101)

        repeated = fit(feature_identical_trade_corpus(incoming_offset=1.0))
        self.assertEqual(repeated.diagnostics.held_out_distinct_perturbation_count, 1)
        self.assertEqual(repeated.model.calibration.status, CalibrationStatus.SURROGATE)

    def test_no_holdout_is_unvalidated_and_fit_id_includes_diagnostics(self):
        result = fit(corpus(include_holdout=False))
        self.assertEqual(result.model.calibration.status, CalibrationStatus.UNVALIDATED)
        self.assertIsNone(result.diagnostics.holdout_max_delta_error)
        self.assertIn(result.config.config_id, result.config.to_record().values())
        with self.assertRaisesRegex(ValueError, "fit config"):
            replace(result, residual_weights={"bogus": 1.0})

    def test_rank_deficiency_fails_without_regularization(self):
        base = corpus(include_holdout=False)
        duplicated = tuple(
            PlayerFeatureVector(
                row.player_id,
                row.eligible_positions,
                {"copy": row.values["value"], **dict(row.values)},
            )
            for row in base.player_features
        )
        deficient = CalibrationCorpus(
            snapshot_id=base.snapshot_id,
            season=base.season,
            scoring_profile_id=base.scoring_profile_id,
            role_definitions=base.role_definitions,
            player_features=duplicated,
            baseline_rosters=(
                TeamRoster("a", ("p1", "p2", "p5"), 3, 3),
                TeamRoster("b", ("p3", "p4", "p6"), 3, 3),
            ),
            samples=base.samples,
        )
        with self.assertRaisesRegex(ValueError, "rank deficient"):
            fit_strength_surrogate(
                deficient,
                CalibrationFitConfig(("copy", "value"), ("role",), ridge_penalty=0),
                bundle=CURRENT_BUNDLE_FINGERPRINT,
                response_schema_sha256=SCHEMA_SHA,
                captured_at=datetime.now(timezone.utc),
            )

    def test_residual_only_signal_can_initialize_with_zero_role_feature(self):
        base = corpus(include_holdout=False)
        features = tuple(feature(row.player_id, row.values["value"], role_value=0) for row in base.player_features)
        residual_only = CalibrationCorpus(
            snapshot_id=base.snapshot_id,
            season=base.season,
            scoring_profile_id=base.scoring_profile_id,
            role_definitions=base.role_definitions,
            player_features=features,
            baseline_rosters=(
                TeamRoster("a", ("p1", "p2", "p5"), 3, 3),
                TeamRoster("b", ("p3", "p4", "p6"), 3, 3),
            ),
            samples=base.samples,
        )
        result = fit(residual_only, ridge=1e-8)
        self.assertGreater(result.residual_weights["value"], 0)

    def test_tiny_but_identifiable_column_is_scaled_before_solving(self):
        role = RoleDefinition("RB_START_1", RoleKind.STARTER, "RB", frozenset({"RB"}))
        tiny = CalibrationCorpus(
            snapshot_id="tiny",
            season=2026,
            scoring_profile_id="scoring-1",
            role_definitions=(role,),
            player_features=(
                PlayerFeatureVector("p1", frozenset({"RB"}), {"role": 1, "tiny": 1e-200}),
                PlayerFeatureVector("p2", frozenset({"RB"}), {"role": 3, "tiny": 2e-200}),
            ),
            baseline_rosters=(TeamRoster("a", ("p1",), 1, 1), TeamRoster("b", ("p2",), 1, 1)),
            samples=(sample("a", "a", ("p1",), 50), sample("b", "b", ("p2",), 100)),
        )
        result = fit_strength_surrogate(
            tiny,
            CalibrationFitConfig(("tiny",), ("role",), ridge_penalty=0),
            bundle=CURRENT_BUNDLE_FINGERPRINT,
            response_schema_sha256=SCHEMA_SHA,
            captured_at=datetime.now(timezone.utc),
        )
        self.assertTrue(result.diagnostics.identifiable)
        self.assertLess(result.diagnostics.training_max_absolute_error, 1e-6)


if __name__ == "__main__":
    unittest.main()
